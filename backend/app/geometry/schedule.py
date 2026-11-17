"""When the next geometric check is due (pure function).

A check becomes due by calendar time, by running hours taken from monitoring,
or at once after an event: an alarm matching the policy patterns (servo, overload)
or an operator's collision mark.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

SOON_DAYS = 14  # "due soon" this many days before the date
SOON_HOURS_SHARE = 0.1  # ... or when this share of the running hours interval is left


@dataclass(frozen=True, slots=True)
class Policy:
    interval_days: int | None = 180
    interval_run_hours: float | None = None
    on_alarm: bool = True
    on_collision: bool = True
    # an alarm triggers a check if its code starts with a pattern, its category equals one,
    # or its message contains one (case-insensitive)
    alarm_patterns: tuple[str, ...] = ("SV", "servo", "overload", "OVC", "OVL")
    is_default: bool = False

    def matches(self, code: str | None, category: str | None, message: str | None) -> bool:
        for pattern in (p.strip().lower() for p in self.alarm_patterns if p.strip()):
            if (code or "").lower().startswith(pattern) or (category or "").lower() == pattern:
                return True
            if pattern in (message or "").lower():
                return True
        return False


DEFAULT_POLICY = Policy(is_default=True)


@dataclass(frozen=True, slots=True)
class Trigger:
    kind: str  # alarm | collision
    ts: datetime
    code: str | None = None
    message: str | None = None


@dataclass(slots=True)
class NextCheck:
    status: str  # never | ok | soon | overdue | event
    last_test_at: datetime | None
    due_date: datetime | None = None
    run_hours_since: float | None = None
    run_hours_left: float | None = None
    triggers: list[Trigger] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_test_at": self.last_test_at,
            "due_date": self.due_date,
            "run_hours_since": self.run_hours_since,
            "run_hours_left": self.run_hours_left,
            "triggers": [{"kind": t.kind, "ts": t.ts, "code": t.code, "message": t.message} for t in self.triggers],
        }


def next_check(
    policy: Policy,
    last_test_at: datetime | None,
    run_seconds_since: float | None,
    alarms: Sequence[tuple[datetime, str | None, str | None, str | None]],
    collisions: Sequence[tuple[datetime, str | None]],
    now: datetime,
) -> NextCheck:
    """``alarms``: (ts, code, category, message) raised after the last test; ``collisions``: (ts, comment)."""
    if last_test_at is None:
        return NextCheck("never", None)
    triggers: list[Trigger] = []
    if policy.on_alarm:
        triggers += [
            Trigger("alarm", ts, code, message)
            for ts, code, category, message in alarms
            if ts > last_test_at and policy.matches(code, category, message)
        ]
    if policy.on_collision:
        triggers += [Trigger("collision", ts, None, comment) for ts, comment in collisions if ts > last_test_at]
    triggers.sort(key=lambda t: t.ts)

    result = NextCheck("ok", last_test_at, triggers=triggers)
    soon = False
    if policy.interval_days:
        result.due_date = last_test_at + timedelta(days=policy.interval_days)
        if now >= result.due_date:
            result.status = "overdue"
        soon = result.due_date - now <= timedelta(days=SOON_DAYS)
    if policy.interval_run_hours:
        result.run_hours_since = (run_seconds_since or 0.0) / 3600
        result.run_hours_left = policy.interval_run_hours - result.run_hours_since
        if result.run_hours_left <= 0:
            result.status = "overdue"
        soon = soon or result.run_hours_left <= policy.interval_run_hours * SOON_HOURS_SHARE
    if triggers:
        result.status = "event"
    elif result.status == "ok" and soon:
        result.status = "soon"
    return result
