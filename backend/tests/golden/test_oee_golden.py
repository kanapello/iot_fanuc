"""OEE against hand calculation (ARCHITECTURE.md 6.1)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from app.oee.calc import aggregate, compute_oee
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
    result = compute_oee(windows, intervals, parts, {"O1": 60.0, "O2": 90.0}, scrap=5, now=at(600))

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
        windows, intervals, parts, {"O1": 300.0, "O9": 300.0}, 0, at(180), planned_downtime=[(at(60), at(120))]
    )
    assert planned.planned_time_s == 120 * 60  # 180 elapsed - 60 planned downtime
    assert planned.run_time_s == 120 * 60
    assert planned.stop_s == 0
    assert planned.availability == pytest.approx(1.0)
    assert planned.performance == pytest.approx(14 * 300 / 7200)
    assert planned.quality == pytest.approx(1.0)

    # without a norm for O9 performance (and OEE) cannot be calculated, availability still can
    no_norm = compute_oee(windows, intervals, parts, {"O1": 300.0}, 0, at(180))
    assert no_norm.no_standard is True
    assert no_norm.availability == pytest.approx(120 / 180)
    assert no_norm.performance is None and no_norm.oee is None


def test_aggregate_sums_components_not_ratios() -> None:
    windows_a, windows_b = [(at(0), at(60))], [(at(60), at(120))]
    a = compute_oee(windows_a, [("RUN", at(0), at(60))], [(at(10), "O1", 60)], {"O1": 60.0}, 0, at(999))
    b = compute_oee(windows_b, [("STOP", at(60), at(120))], [], {"O1": 60.0}, 0, at(999))
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
