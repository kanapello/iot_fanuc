"""Golden tests of the timeline rules: scenario -> expected intervals (ARCHITECTURE.md 5.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.timeline.builder import Interval, Rules, Sample, build_intervals, part_events

T0 = datetime(2026, 11, 16, 8, 0, 0, tzinfo=UTC)
RULES = Rules()  # offline 30 s, idle->stop 120 s, min interval 5 s


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def samples(*spans: tuple[float, float, dict[str, object]], step: float = 10.0) -> list[Sample]:
    """Keyframes every ``step`` seconds inside each (start, end, signals) span."""
    result = []
    for start, end, signals in spans:
        t = start
        while t < end:
            result.append(Sample(ts=at(t), **{"mode": "MEM", "run": "STOP", **signals}))  # type: ignore[arg-type]
            t += step
    return result


def shape(intervals: list[Interval]) -> list[tuple[str, float, float | None]]:
    return [
        (i.state, (i.start - T0).total_seconds(), None if i.end is None else (i.end - T0).total_seconds())
        for i in intervals
    ]


RUNNING = {"run": "START"}


def test_normal_operation_with_short_pauses() -> None:
    data = samples(
        (0, 300, RUNNING),
        (300, 340, {}),  # 40 s part change -> IDLE
        (340, 600, RUNNING),
        (600, 603, {"run": "HOLD"}),  # 3 s feed hold is shorter than min_interval -> glued to RUN
        (603, 900, RUNNING),
        step=1,
    )
    assert shape(build_intervals(data, RULES, at(0), at(900))) == [
        ("RUN", 0, 300),
        ("IDLE", 300, 340),
        ("RUN", 340, None),
    ]


def test_long_downtime_becomes_stop_after_threshold() -> None:
    data = samples((0, 100, RUNNING), (100, 500, {}), (500, 560, RUNNING))
    assert shape(build_intervals(data, RULES, at(0), at(560))) == [
        ("RUN", 0, 100),
        ("IDLE", 100, 220),  # first 120 s of the stretch
        ("STOP", 220, 500),  # the rest needs a reason from the operator
        ("RUN", 500, None),
    ]


def test_alarm_and_emergency_override_everything() -> None:
    data = samples(
        (0, 60, RUNNING),
        (60, 150, {"alarm": True}),
        (150, 180, {}),
        (180, 240, RUNNING),
        (240, 300, {"run": "START", "emergency": True}),  # e-stop wins even over run=START
    )
    assert shape(build_intervals(data, RULES, at(0), at(300))) == [
        ("RUN", 0, 60),
        ("ALARM", 60, 150),
        ("IDLE", 150, 180),
        ("RUN", 180, 240),
        ("ALARM", 240, None),
    ]


def test_connection_loss_and_silence_are_offline() -> None:
    data = [
        *samples((0, 100, RUNNING)),
        Sample(ts=at(100), connected=False),  # collector reports: machine unreachable
        *samples((160, 200, RUNNING)),
        # then the collector itself goes silent: last sample at 190 s
    ]
    assert shape(build_intervals(data, RULES, at(0), at(400))) == [
        ("RUN", 0, 100),
        ("OFFLINE", 100, 160),
        ("RUN", 160, 220),  # the last sample is trusted for offline_timeout (30 s)
        ("OFFLINE", 220, None),
    ]


def test_setup_by_operator_mark_and_by_mode() -> None:
    data = samples((0, 60, RUNNING), (60, 200, {}), (200, 300, {"mode": "JOG"}), (300, 360, RUNNING))
    marks = [(at(90), at(150))]
    assert shape(build_intervals(data, RULES, at(0), at(360), marks=marks)) == [
        ("RUN", 0, 60),
        ("IDLE", 60, 90),
        ("SETUP", 90, 150),  # operator pressed "setup"
        ("IDLE", 150, 200),
        ("SETUP", 200, 300),  # JOG mode
        ("RUN", 300, None),
    ]


def test_incremental_rebuild_continues_an_open_stretch() -> None:
    """The worker extends the tail: a stretch that began earlier keeps its start."""
    full = samples((0, 100, RUNNING), (100, 400, {}))
    expected = shape(build_intervals(full, RULES, at(0), at(400)))
    assert expected == [("RUN", 0, 100), ("IDLE", 100, 220), ("STOP", 220, None)]

    # same data seen in two steps: first up to 150 s, then the tail from 150 s
    first = build_intervals([s for s in full if s.ts < at(150)], RULES, at(0), at(150))
    assert shape(first) == [("RUN", 0, 100), ("IDLE", 100, None)]
    tail = [s for s in full if s.ts >= at(150)]
    prev = next(s for s in reversed(full) if s.ts < at(150))
    carry = Interval("NOT_RUN", at(100), None)
    second = build_intervals(tail, RULES, at(150), at(400), prev=prev, carry=carry)
    assert shape([first[0], *second]) == expected


def test_part_counter_with_reset() -> None:
    counts = [(0, 100), (10, 100), (20, 101), (30, 103), (40, 0), (50, 0), (60, 1), (70, 2), (80, None), (90, 2)]
    data = [Sample(ts=at(t), run="START", mode="MEM", program="O1", part_count=c) for t, c in counts]
    events = part_events(data, prev_count=99)
    assert [((e.ts - T0).total_seconds(), e.delta) for e in events] == [
        (0, 1),  # 99 -> 100, continues the counter seen before the window
        (20, 1),
        (30, 2),
        # 103 -> 0 is a reset by the operator: no negative output
        (60, 1),
        (70, 1),
    ]
    assert sum(e.delta for e in events) == 6
