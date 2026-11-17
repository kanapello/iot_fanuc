"""OEE against hand calculation (ARCHITECTURE.md 6.1)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from app.oee.calc import NormTable, aggregate, compute_oee
from app.oee.shifts import ShiftTemplate, shift_instances

T0 = datetime(2026, 11, 16, 5, 0, 0, tzinfo=UTC)  # 08:00 Moscow time


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def test_shift_oee_matches_manual_calculation() -> None:
    # Shift 08:00-12:00 (240 min) with a 20 min planned break at 10:00.
    windows = [(at(0), at(120)), (at(140), at(240))]
    intervals = [
        ("RUN", at(0), at(100)),
        ("IDLE", at(100), at(102)),
        ("STOP", at(102), at(120)),
        ("STOP", at(120), at(140)),  # the break itself: outside planned time
        ("RUN", at(140), at(200)),
        ("ALARM", at(200), at(210)),
        ("RUN", at(210), at(240)),
    ]
    # 150 parts of O1 (norm 60 s) and 20 parts of O2 (norm 90 s); 2 parts made during the break are not counted
    parts = [
        *[(at(0.5 + i * 0.66), "O1", 1) for i in range(150)],
        (at(130), "O1", 2),
        *[(at(141 + i * 4.5), "O2", 1) for i in range(20)],
    ]
    result = compute_oee(windows, intervals, parts, NormTable.constant({"O1": 60.0, "O2": 90.0}), scrap=5, now=at(600))

    assert result.planned_time_s == 220 * 60  # 240 - 20
    assert result.run_time_s == 190 * 60  # 100 + 60 + 30
    assert (result.idle_s, result.stop_s, result.alarm_s) == (2 * 60, 18 * 60, 10 * 60)
    assert (result.total_parts, result.scrap_parts, result.good_parts) == (170, 5, 165)
    assert result.ideal_time_s == 150 * 60 + 20 * 90  # 10800 s
    assert result.availability == pytest.approx(190 / 220)
    assert result.performance == pytest.approx(10800 / 11400)
    assert result.quality == pytest.approx(165 / 170)
    assert result.oee == pytest.approx((190 / 220) * (10800 / 11400) * (165 / 170))  # 0.794


def test_planned_downtime_missing_norm_and_running_shift() -> None:
    windows = [(at(0), at(480))]
    intervals = [("RUN", at(0), at(60)), ("STOP", at(60), at(120)), ("RUN", at(120), None)]
    parts = [(at(30), "O1", 10), (at(150), "O9", 4)]

    # 60 min "changeover" annotated as planned; the shift is still running: now = 180 min
    planned = compute_oee(
        windows,
        intervals,
        parts,
        NormTable.constant({"O1": 300.0, "O9": 300.0}),
        0,
        at(180),
        planned_downtime=[(at(60), at(120))],
    )
    assert planned.planned_time_s == 120 * 60  # 180 elapsed - 60 planned downtime
    assert planned.run_time_s == 120 * 60
    assert planned.stop_s == 0
    assert planned.availability == pytest.approx(1.0)
    assert planned.performance == pytest.approx(14 * 300 / 7200)
    assert planned.quality == pytest.approx(1.0)

    # without a norm for O9 performance (and OEE) cannot be calculated, availability still can
    no_norm = compute_oee(windows, intervals, parts, NormTable.constant({"O1": 300.0}), 0, at(180))
    assert no_norm.no_standard is True
    assert no_norm.availability == pytest.approx(120 / 180)
    assert no_norm.performance is None and no_norm.oee is None


def test_aggregate_sums_components_not_ratios() -> None:
    windows_a, windows_b = [(at(0), at(60))], [(at(60), at(120))]
    a = compute_oee(
        windows_a, [("RUN", at(0), at(60))], [(at(10), "O1", 60)], NormTable.constant({"O1": 60.0}), 0, at(999)
    )
    b = compute_oee(windows_b, [("STOP", at(60), at(120))], [], NormTable.constant({"O1": 60.0}), 0, at(999))
    assert (a.oee, b.oee) == (pytest.approx(1.0), 0.0)
    total = aggregate([a.as_dict(), b.as_dict()])
    assert total["availability"] == pytest.approx(0.5)
    assert total["performance"] == pytest.approx(1.0)
    assert total["oee"] == pytest.approx(0.5)


def test_two_twelve_hour_shifts_in_local_time() -> None:
    templates = [
        ShiftTemplate("День", time(8), time(20), breaks=((time(12), time(12, 45)),)),
        ShiftTemplate("Ночь", time(20), time(8), breaks=((time(0), time(0, 45)),)),
    ]
    day = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    shifts = shift_instances(templates, "Europe/Moscow", day, day + timedelta(hours=12))
    assert [(s.name, s.start, s.end) for s in shifts] == [
        ("День", datetime(2026, 11, 16, 5, 0, tzinfo=UTC), datetime(2026, 11, 16, 17, 0, tzinfo=UTC)),
        ("Ночь", datetime(2026, 11, 16, 17, 0, tzinfo=UTC), datetime(2026, 11, 17, 5, 0, tzinfo=UTC)),
    ]
    night = shifts[1]
    assert night.breaks == ((datetime(2026, 11, 16, 21, 0, tzinfo=UTC), datetime(2026, 11, 16, 21, 45, tzinfo=UTC)),)
    assert sum((e - s).total_seconds() for s, e in night.windows) == (12 * 60 - 45) * 60


def test_scrap_recorded_later_keeps_shift_and_hourly_quality_equal() -> None:
    """Scrap is taken as recorded: the sum of hours gives the same quality as the shift (1.2)."""
    norms = NormTable.constant({"O1": 60.0})
    intervals = [("RUN", at(0), at(180))]
    # 50 parts in the first hour, 10 in the second, none in the third
    parts = [*[(at(1 + i), "O1", 1) for i in range(50)], *[(at(61 + i), "O1", 1) for i in range(10)]]
    # the operator records 12 rejected parts in the second hour and 3 more in the third
    scrap = [(at(100), 12), (at(150), 3)]

    def bucket(start: float, end: float):  # type: ignore[no-untyped-def]
        scrapped = sum(qty for ts, qty in scrap if at(start) <= ts < at(end))
        return compute_oee([(at(start), at(end))], intervals, parts, norms, scrapped, at(999))

    hours = [bucket(0, 60), bucket(60, 120), bucket(120, 180)]
    shift = bucket(0, 180)
    assert [h.scrap_parts for h in hours] == [0, 12, 3]  # not clipped to the 10 and 0 parts of those hours
    assert hours[1].quality == 0.0 and hours[2].quality is None  # a single hour cannot go below zero
    day = aggregate([h.as_dict() for h in hours])
    assert (shift.total_parts, shift.scrap_parts, shift.good_parts) == (60, 15, 45)
    assert (day["total_parts"], day["scrap_parts"], day["good_parts"]) == (60, 15, 45)
    assert day["quality"] == pytest.approx(shift.quality) == pytest.approx(0.75)
    assert day["oee"] == pytest.approx(shift.oee)


def test_new_norm_does_not_change_past_periods() -> None:
    """A norm applies from its valid_from on (1.8)."""
    windows = [(at(0), at(120))]
    intervals = [("RUN", at(0), at(120))]
    parts = [(at(10), "O1", 30), (at(70), "O1", 30)]
    old = NormTable([("O1", None, 60.0, False)])
    changed = NormTable([("O1", None, 60.0, False), ("O1", at(60), 90.0, False)])

    first_hour = [(at(0), at(60))]
    before = compute_oee(first_hour, intervals, parts, old, 0, at(999))
    after = compute_oee(first_hour, intervals, parts, changed, 0, at(999))
    assert before.as_dict() == after.as_dict()  # the past hour is untouched by the new norm
    assert before.performance == pytest.approx(30 * 60 / 3600)

    whole = compute_oee(windows, intervals, parts, changed, 0, at(999))
    assert whole.ideal_time_s == 30 * 60 + 30 * 90  # each part with the norm valid when it was made

    # a machine-specific norm wins over the common one, each within its own validity
    specific = NormTable([("O1", None, 60.0, False), ("O1", at(60), 40.0, True), ("O1", at(100), 50.0, False)])
    assert [specific.lookup("O1", at(m)) for m in (10, 70, 110)] == [60.0, 40.0, 40.0]
    assert specific.lookup("O9", at(10)) is None
