"""Golden tests of the cycle split: raw samples -> timeline and part events -> cycles (ADR 0008)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.timeline.builder import Rules, Sample, build_intervals, part_events
from app.timeline.cycles import Cycle, build_cycles, pause_threshold, split

T0 = datetime(2026, 11, 16, 8, 0, 0, tzinfo=UTC)
RULES = Rules()  # offline 30 s, idle->stop 120 s, min interval 5 s


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class Machine:
    """Writes one sample per second: what a collector at 1 s would send."""

    def __init__(self, program: str = "O1", count: int = 100) -> None:
        self.t = 0
        self.program = program
        self.count = count
        self.data: list[Sample] = []

    def run(self, seconds: int, parts: int = 1) -> Machine:
        """Machining; the counter grows by ``parts`` when it ends (M30): the next sample shows the part.

        ``parts=0`` — machining that is interrupted before the part is done.
        """
        self._write(seconds, "START")
        self.count += parts
        return self

    def stand(self, seconds: int, **signals: object) -> Machine:
        self._write(seconds, "STOP", **signals)
        return self

    def silent(self, seconds: int) -> Machine:
        """No samples at all: the collector lost the control."""
        self.t += seconds
        return self

    def _write(self, seconds: int, run: str, **signals: object) -> None:
        for _ in range(seconds):
            sample = {"mode": "MEM", "run": run, "program": self.program, "part_count": self.count, **signals}
            self.data.append(Sample(ts=at(self.t), **sample))  # type: ignore[arg-type]
            self.t += 1

    def cycles(self) -> list[Cycle]:
        intervals = build_intervals(self.data, RULES, at(0), at(self.t))
        events = part_events(self.data, None)
        return build_cycles(events, None, intervals, at(self.t))


def shape(cycles: list[Cycle]) -> list[tuple[str | None, int, float, float, float, str]]:
    """(program, parts, machine time, gap, interruptions, kind) of each cycle."""
    return [(c.program, c.parts, c.machine_s, c.gap_s, c.interrupt_s, c.kind) for c in cycles]


def test_normal_work() -> None:
    """Machining 60 s, part change 20 s: every cycle is 60 s of RUN and a 20 s gap."""
    m = Machine()
    for _ in range(5):
        m.run(60).stand(20)
    cycles = m.cycles()
    assert shape(cycles) == [("O1", 1, 60, 20, 0, "normal")] * 4
    assert [(c.end - c.start).total_seconds() for c in cycles] == [80] * 4
    assert cycles[0].end == at(60) + timedelta(seconds=80)  # a cycle ends with its part


def test_long_pause_is_downtime_not_part_change() -> None:
    m = Machine()
    m.run(60).stand(20).run(60).stand(1800).run(60).stand(20).run(60).stand(20)
    cycles = m.cycles()
    assert [c.gap_s for c in cycles] == [20, 1800, 20]
    # the gap longer than the threshold is downtime: max(3 x median 60 s, 10 min) = 600 s
    threshold = pause_threshold([c.machine_s for c in cycles], None)
    assert threshold == 600
    assert [(s.load_s, s.pause_s) for s in split(cycles, threshold)] == [(20, 0), (0, 1800), (20, 0)]
    # a machine setting overrides the default
    assert [(s.load_s, s.pause_s) for s in split(cycles, pause_threshold([], 15))] == [(0, 20), (0, 1800), (0, 20)]
    # the long gap is IDLE then STOP on the timeline, but inside the gap it is not an interruption
    assert cycles[1].interrupt_s == 0 and cycles[1].kind == "normal"


def test_interruptions_inside_machining() -> None:
    """A feed hold and an alarm after machining started are interruptions, not part change."""
    m = Machine()
    m.run(60).stand(20)
    m.run(30, parts=0).stand(40).run(30)  # a 40 s feed hold in the middle of machining
    m.stand(20).run(20, parts=0)
    m.stand(50, alarm=True)
    m.run(40).stand(10)
    cycles = m.cycles()
    assert shape(cycles) == [("O1", 1, 60, 20, 40, "normal"), ("O1", 1, 60, 20, 50, "normal")]
    assert (cycles[0].idle_s, cycles[1].alarm_s) == (40, 50)


def test_program_change_makes_the_first_part_set_up() -> None:
    m = Machine()
    m.run(60).stand(20).run(60).stand(300)
    m.program = "O2"
    m.stand(10).run(90).stand(20).run(90).stand(20).run(90).stand(20)
    assert [(c.program, c.kind) for c in m.cycles()] == [
        ("O1", "normal"),
        ("O2", "setup"),  # the first part after the program change
        ("O2", "normal"),
        ("O2", "normal"),
    ]


def test_connection_loss_inside_a_cycle_makes_it_incomplete() -> None:
    m = Machine()
    m.run(60).stand(20).run(60).stand(20)
    m.run(20, parts=0).silent(90).run(40).stand(20)  # 90 s without data in the middle of machining
    m.run(60).stand(10)
    cycles = m.cycles()
    assert [c.kind for c in cycles] == ["normal", "incomplete", "normal"]
    assert cycles[1].offline_s > 0
    assert cycles[2].offline_s == 0


def test_counter_reset_is_not_a_part() -> None:
    """The operator resets the counter between parts: no extra cycle, the cycle across the reset is normal."""
    m = Machine(count=7)
    m.run(60).stand(10)
    m.count = 0  # reset during the part change
    m.stand(10).run(60).stand(20).run(60).stand(10)
    cycles = m.cycles()
    assert shape(cycles) == [("O1", 1, 60, 20, 0, "normal")] * 2


def test_several_parts_in_one_cycle() -> None:
    """A fixture with four workpieces: the counter grows by 4 at once, which is one cycle of four parts."""
    m = Machine()
    m.run(240, parts=4).stand(60).run(240, parts=4).stand(60).run(240, parts=4).stand(10)
    assert shape(m.cycles()) == [("O1", 4, 240, 60, 0, "normal")] * 2


def test_first_part_has_no_cycle_and_an_unfinished_one_is_not_stored() -> None:
    m = Machine()
    m.run(60).stand(20).run(30, parts=0)  # the second part is still being machined
    assert m.cycles() == []
