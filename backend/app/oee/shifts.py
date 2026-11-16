"""Shift instances from shift templates (local wall-clock times of the site)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

Window = tuple[datetime, datetime]


@dataclass(frozen=True, slots=True)
class ShiftTemplate:
    name: str
    start_time: time
    end_time: time
    days_of_week: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7)  # ISO weekday of the shift start
    breaks: tuple[tuple[time, time], ...] = ()

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ShiftTemplate:
        breaks = tuple(
            (time.fromisoformat(b["start"]), time.fromisoformat(b["end"])) for b in row.get("planned_breaks") or []
        )
        return cls(row["name"], row["start_time"], row["end_time"], tuple(row["days_of_week"]), breaks)


@dataclass(frozen=True, slots=True)
class Shift:
    name: str
    start: datetime
    end: datetime
    breaks: tuple[Window, ...]

    @property
    def windows(self) -> list[Window]:
        """Planned production windows: the shift without its planned breaks."""
        return subtract([(self.start, self.end)], list(self.breaks))


def subtract(windows: Sequence[Window], holes: Sequence[Window]) -> list[Window]:
    result = list(windows)
    for hole_start, hole_end in holes:
        next_result: list[Window] = []
        for start, end in result:
            if hole_end <= start or hole_start >= end:
                next_result.append((start, end))
                continue
            if hole_start > start:
                next_result.append((start, hole_start))
            if hole_end < end:
                next_result.append((hole_end, end))
        result = next_result
    return result


def clip(windows: Sequence[Window], start: datetime, end: datetime) -> list[Window]:
    return [(max(s, start), min(e, end)) for s, e in windows if min(e, end) > max(s, start)]


def _local(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz).astimezone(UTC)


def shift_instances(templates: Sequence[ShiftTemplate], timezone: str, start: datetime, end: datetime) -> list[Shift]:
    """Shifts overlapping [start, end), ordered by start."""
    tz = ZoneInfo(timezone)
    first_day = start.astimezone(tz).date() - timedelta(days=1)
    last_day = end.astimezone(tz).date()
    shifts: list[Shift] = []
    day = first_day
    while day <= last_day:
        for template in templates:
            if day.isoweekday() not in template.days_of_week:
                continue
            overnight = template.end_time <= template.start_time
            shift_start = _local(day, template.start_time, tz)
            shift_end = _local(day + timedelta(days=1) if overnight else day, template.end_time, tz)
            if shift_end <= start or shift_start >= end:
                continue
            breaks = []
            for break_start, break_end in template.breaks:
                break_day = day + timedelta(days=1) if overnight and break_start < template.start_time else day
                b0 = _local(break_day, break_start, tz)
                b1 = _local(break_day + timedelta(days=1) if break_end <= break_start else break_day, break_end, tz)
                breaks.append((b0, b1))
            shifts.append(Shift(template.name, shift_start, shift_end, tuple(breaks)))
        day += timedelta(days=1)
    return sorted(shifts, key=lambda s: s.start)
