"""Rules of the geometric accuracy module: parameter keys, tolerance choice, next check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.geometry.catalog import BY_CODE, InvalidResultError, check_result
from app.geometry.schedule import DEFAULT_POLICY, Policy, next_check
from app.geometry.tolerances import Tolerance, exceeds, pick

T0 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)


def test_parameter_keys_follow_the_plane() -> None:
    assert BY_CODE["backlash"].keys("XY") == [("X", None), ("Y", None)]
    assert BY_CODE["backlash"].keys("ZX") == [("Z", None), ("X", None)]
    assert BY_CODE["circularity"].keys("YZ") == [(None, None), (None, "CW"), (None, "CCW")]
    assert BY_CODE["squareness"].keys("XY") == [(None, None)]
    assert check_result("XY", "scaling_error", "Y", None).unit == "ppm"
    assert check_result("XY", "servo_mismatch", None, None).unit == "ms"
    assert check_result("ZX", "squareness", None, None).unit == "µm/m"
    for wrong in [("XY", "backlash", "Z", None), ("XY", "squareness", "X", None), ("XY", "backlash", "X", "CW"),
                  ("XY", "circular_hysteresis", None, "CW"), ("XY", "flatness", None, None)]:  # fmt: skip
        with pytest.raises(InvalidResultError):
            check_result(*wrong)


def tol(i: int, limit: float, **kwargs: object) -> Tolerance:
    return Tolerance(id=i, limit=limit, unit="µm", **kwargs)  # type: ignore[arg-type]


def test_most_specific_tolerance_wins() -> None:
    rows = [
        tol(1, 20.0, parameter="backlash", machine_model="VF-500"),
        tol(2, 15.0, parameter="backlash", machine_model="VF-500", plane="XY"),
        tol(3, 12.0, parameter="backlash", machine_model="VF-500", plane="XY", axis="X"),
        tol(4, 8.0, parameter="backlash", machine_id=7),  # the machine itself, any plane and axis
        tol(5, 30.0, parameter="backlash", machine_model="OTHER"),
        tol(6, 99.0, parameter="backlash", machine_id=8),
    ]
    assert pick(rows, 7, "VF-500", "XY", "backlash", "X") == rows[3]  # machine beats a narrower model row
    assert pick(rows, 9, "VF-500", "XY", "backlash", "X") == rows[2]
    assert pick(rows, 9, "VF-500", "XY", "backlash", "Y") == rows[1]
    assert pick(rows, 9, "VF-500", "ZX", "backlash", "Z") == rows[0]
    assert pick(rows, 9, None, "XY", "backlash", "X") is None  # a machine without a model gets no model rows
    assert pick(rows, 9, "VF-500", "XY", "straightness", "X") is None


def test_exceedance_is_checked_on_magnitude() -> None:
    limit = tol(1, 15.0, parameter="squareness", machine_id=1)
    assert not exceeds(15.0, limit) and not exceeds(-15.0, limit)
    assert exceeds(15.1, limit) and exceeds(-15.1, limit)
    assert not exceeds(1000.0, None)


def test_next_check_by_date_and_running_hours() -> None:
    assert next_check(DEFAULT_POLICY, None, None, [], [], T0).status == "never"

    policy = Policy(interval_days=180, interval_run_hours=1000)
    hours = 3600.0
    ok = next_check(policy, T0, 100 * hours, [], [], T0 + timedelta(days=100))
    assert (ok.status, ok.due_date, ok.run_hours_left) == ("ok", T0 + timedelta(days=180), 900)
    assert next_check(policy, T0, 100 * hours, [], [], T0 + timedelta(days=170)).status == "soon"  # 10 days left
    assert next_check(policy, T0, 950 * hours, [], [], T0 + timedelta(days=10)).status == "soon"  # 5 % of hours left
    assert next_check(policy, T0, 100 * hours, [], [], T0 + timedelta(days=180)).status == "overdue"
    assert next_check(policy, T0, 1000 * hours, [], [], T0 + timedelta(days=10)).status == "overdue"
    hours_only = Policy(interval_days=None, interval_run_hours=500)
    late = next_check(hours_only, T0, 600 * hours, [], [], T0 + timedelta(days=900))
    assert (late.status, late.due_date, late.run_hours_left) == ("overdue", None, -100)


def test_events_make_the_check_due_at_once() -> None:
    policy = Policy(interval_days=365)
    now = T0 + timedelta(days=30)
    alarms = [
        (T0 - timedelta(days=1), "SV0401", "servo", "V-READY OFF"),  # before the test: ignored
        (T0 + timedelta(days=2), "OT0500", "OT", "OVER TRAVEL"),  # not a servo or overload alarm
        (T0 + timedelta(days=3), "SP9001", "SP", "SPINDLE OVERLOAD"),  # overload in the message
        (T0 + timedelta(days=5), "SV0436", None, "SOFTTHERMAL (OVC)"),
    ]
    collisions = [(T0 + timedelta(days=4), "Удар фрезы о тиски")]
    check = next_check(policy, T0, 0, alarms, collisions, now)
    assert check.status == "event"
    assert [(t.kind, t.code) for t in check.triggers] == [("alarm", "SP9001"), ("collision", None), ("alarm", "SV0436")]

    quiet = Policy(interval_days=365, on_alarm=False, on_collision=False)
    assert next_check(quiet, T0, 0, alarms, collisions, now).status == "ok"
    only_servo = Policy(interval_days=365, on_collision=False, alarm_patterns=("servo",))
    assert (
        next_check(only_servo, T0, 0, alarms, collisions, now).triggers == []
    )  # category "servo" only before the test
