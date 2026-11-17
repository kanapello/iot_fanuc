"""Pure OEE calculation (ARCHITECTURE.md 6.1)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.oee.shifts import Window, clip, subtract
from app.timeline.builder import ALARM, IDLE, OFFLINE, RUN, SETUP, STOP

_STATE_FIELDS = {
    RUN: "run_time_s",
    IDLE: "idle_s",
    STOP: "stop_s",
    ALARM: "alarm_s",
    SETUP: "setup_s",
    OFFLINE: "offline_s",
}
SUM_FIELDS = (
    "planned_time_s", "run_time_s", "idle_s", "stop_s", "alarm_s", "setup_s", "offline_s",
    "total_parts", "good_parts", "scrap_parts", "ideal_time_s",
)  # fmt: skip


class NormTable:
    """Cycle norms (ideal seconds per part) with their validity periods.

    A norm applies to parts made at or after its ``valid_from`` until a newer
    one starts, so changing a norm never rewrites the past. A machine-specific
    norm wins over the common one of the same program.
    """

    def __init__(self, rows: Iterable[tuple[str, datetime | None, float, bool]] = ()) -> None:
        """rows: (program, valid_from or None for "always", seconds per part, machine-specific)."""
        self._rows: dict[str, list[tuple[bool, datetime | None, float]]] = {}
        for program, valid_from, seconds, specific in rows:
            self._rows.setdefault(program, []).append((specific, valid_from, seconds))

    @classmethod
    def constant(cls, norms: Mapping[str, float]) -> NormTable:
        return cls((program, None, seconds, False) for program, seconds in norms.items())

    def lookup(self, program: str | None, at: datetime) -> float | None:
        best: tuple[bool, datetime | None, float] | None = None
        for row in self._rows.get(program or "", ()):
            specific, valid_from, _ = row
            if valid_from is not None and valid_from > at:
                continue
            if best is None or (specific, valid_from is not None, valid_from) > (best[0], best[1] is not None, best[1]):
                best = row
        return None if best is None else best[2]


@dataclass(slots=True)
class OeeResult:
    planned_time_s: float = 0.0
    run_time_s: float = 0.0
    idle_s: float = 0.0
    stop_s: float = 0.0
    alarm_s: float = 0.0
    setup_s: float = 0.0
    offline_s: float = 0.0
    total_parts: int = 0
    good_parts: int = 0
    scrap_parts: int = 0
    ideal_time_s: float = 0.0
    no_standard: bool = False
    availability: float | None = None
    performance: float | None = None
    quality: float | None = None
    oee: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def ratios(
    planned_time_s: float, run_time_s: float, ideal_time_s: float, total_parts: int, good_parts: int, no_standard: bool
) -> tuple[float | None, float | None, float | None, float | None]:
    """(availability, performance, quality, oee); None = cannot be calculated.

    Performance above 100 % is not clipped: it points at a wrong cycle norm.
    """
    availability = run_time_s / planned_time_s if planned_time_s > 0 else None
    performance = None if no_standard or run_time_s <= 0 else ideal_time_s / run_time_s
    # scrap may be recorded later than the parts were made, so one hour can show more scrap than parts
    quality = max(0.0, good_parts / total_parts) if total_parts > 0 else None
    if availability is None:
        return None, performance, quality, None
    if availability == 0:
        return 0.0, performance, quality, 0.0
    if performance is None:
        return availability, None, quality, None
    # nothing produced yet -> nothing scrapped
    return availability, performance, quality, availability * performance * (1.0 if quality is None else quality)


def _overlap(windows: Sequence[Window], start: datetime, end: datetime) -> float:
    return sum((min(e, end) - max(s, start)).total_seconds() for s, e in windows if min(e, end) > max(s, start))


def compute_oee(
    windows: Sequence[Window],
    intervals: Iterable[tuple[str, datetime, datetime | None]],
    parts: Iterable[tuple[datetime, str | None, int]],
    norms: NormTable,
    scrap: int,
    now: datetime,
    planned_downtime: Sequence[Window] = (),
) -> OeeResult:
    """OEE of one machine for one period.

    ``windows``: planned production windows of the period (shift minus planned
    breaks). Downtime annotated with a planned reason is removed from them.
    Only time up to ``now`` counts. ``scrap`` is taken as recorded, never clipped
    to the parts of the period: sums over hours then match the shift.
    """
    if not windows:
        return OeeResult()
    period_start, period_end = min(s for s, _ in windows), max(e for _, e in windows)
    effective = clip(subtract(windows, planned_downtime), period_start, min(period_end, now))
    result = OeeResult(planned_time_s=sum((e - s).total_seconds() for s, e in effective))

    for state, start, end in intervals:
        name = _STATE_FIELDS.get(state)
        if name:
            setattr(result, name, getattr(result, name) + _overlap(effective, start, end or now))

    for ts, program, delta in parts:
        if not any(s <= ts < e for s, e in effective):
            continue
        result.total_parts += delta
        norm = norms.lookup(program, ts)
        if norm is None:
            result.no_standard = True
        else:
            result.ideal_time_s += delta * norm
    result.scrap_parts = scrap
    result.good_parts = result.total_parts - result.scrap_parts
    result.availability, result.performance, result.quality, result.oee = ratios(
        result.planned_time_s,
        result.run_time_s,
        result.ideal_time_s,
        result.total_parts,
        result.good_parts,
        result.no_standard,
    )
    return result


def aggregate(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum the components of several OEE rows and re-derive the ratios."""
    total: dict[str, Any] = {name: 0 for name in SUM_FIELDS}
    no_standard = False
    for row in rows:
        for name in SUM_FIELDS:
            total[name] += row[name] or 0
        no_standard = no_standard or bool(row.get("no_standard"))
    total["no_standard"] = no_standard
    total["availability"], total["performance"], total["quality"], total["oee"] = ratios(
        total["planned_time_s"],
        total["run_time_s"],
        total["ideal_time_s"],
        total["total_parts"],
        total["good_parts"],
        no_standard,
    )
    return total
