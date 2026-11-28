"""Cycle analytics by program: what a part takes and how it compares with the norm (ADR 0008).

Only normal cycles count (not the first part after a program change, not cycles with OFFLINE inside).
The gap from a part to the next start is part change up to the pause threshold of the machine
(or, by default, three times the median machine time of the program, at least 10 minutes) and
downtime beyond it; downtime gaps are counted separately and left out of the cycle time.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import service as admin
from app.oee import service as oee
from app.oee.calc import NormTable
from app.timeline import service as timeline
from app.timeline.cycles import NORMAL, Cycle, pause_threshold, split

HISTOGRAM_BINS = 12


def _cycle(row: dict[str, Any]) -> Cycle:
    return Cycle(
        start=row["start_ts"],
        end=row["end_ts"],
        program=row["program"],
        parts=row["parts"],
        machine_s=row["machine_s"],
        gap_s=row["gap_s"],
        idle_s=row["idle_s"],
        stop_s=row["stop_s"],
        alarm_s=row["alarm_s"],
        setup_s=row["setup_s"],
        offline_s=row["offline_s"],
        kind=row["kind"],
    )


def _median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def histogram(values: Sequence[float], bins: int = HISTOGRAM_BINS) -> list[dict[str, float]]:
    """Equal-width bins from the smallest to the largest value, at most ``bins``, each at least a second wide."""
    if not values:
        return []
    low, high = math.floor(min(values)), max(values)
    width = max((high - low) / bins, 1.0)
    count = max(1, min(bins, math.ceil((high - low) / width) or 1))
    counts = [0] * count
    for value in values:
        counts[min(int((value - low) / width), count - 1)] += 1
    return [
        {"from": round(low + i * width, 1), "to": round(low + (i + 1) * width, 1), "count": n}
        for i, n in enumerate(counts)
    ]


def _summary(splits: Sequence[Any]) -> dict[str, Any]:
    """Medians of normal cycles; downtime gaps are left out of the cycle time and of the part change."""
    worked = [s for s in splits if s.pause_s == 0]
    busy = sum(s.cycle.machine_s + s.load_s + s.cycle.interrupt_s for s in worked)
    return {
        "cycles": len(splits),
        "machine_median_s": _median([s.cycle.machine_s for s in splits]),
        "load_median_s": _median([s.load_s for s in worked]),
        "cycle_median_s": _median([s.cycle.machine_s + s.load_s + s.cycle.interrupt_s for s in worked]),
        "interrupt_share": round(sum(s.cycle.interrupt_s for s in worked) / busy, 4) if busy else None,
    }


@dataclass(slots=True)
class _Group:
    """Cycles of one machine and one program in a period, split into machine time, part change and pauses."""

    machine: dict[str, Any]
    program: str | None
    cycles: list[Cycle]
    normal: list[Cycle]
    threshold: float
    fixed: float | None
    splits: list[Any]
    norms: NormTable


async def _groups(
    session: AsyncSession, machines: Sequence[dict[str, Any]], start: datetime, end: datetime
) -> list[_Group]:
    rows = await timeline.list_cycles(session, [m["id"] for m in machines], start, end)
    by_key: dict[tuple[int, str | None], list[Cycle]] = defaultdict(list)
    for row in rows:
        by_key[(row["machine_id"], row["program"])].append(_cycle(row))
    by_id = {m["id"]: m for m in machines}
    norms: dict[int, NormTable] = {}
    result = []
    for (machine_id, program), cycles in sorted(by_key.items(), key=lambda g: (g[0][0], g[0][1] or "")):
        machine = by_id[machine_id]
        if machine_id not in norms:
            norms[machine_id] = NormTable(await admin.cycle_norms(session, machine))
        normal = [c for c in cycles if c.kind == NORMAL]
        fixed = machine.get("cycle_pause_threshold_s")
        threshold = pause_threshold([c.machine_s for c in normal], fixed)
        result.append(
            _Group(machine, program, cycles, normal, threshold, fixed, split(normal, threshold), norms[machine_id])
        )
    return result


def _base(group: _Group, end: datetime) -> dict[str, Any]:
    program, normal = group.program, group.normal
    ratios = []
    for c in normal:
        per_part = group.norms.lookup(program, c.end)
        if per_part:
            ratios.append(c.machine_s / (per_part * c.parts))
    return {
        "machine_id": group.machine["id"],
        "machine_name": group.machine["name"],
        "program": program,
        "total": len(group.cycles),
        "setup": sum(1 for c in group.cycles if c.kind == "setup"),
        "incomplete": sum(1 for c in group.cycles if c.kind == "incomplete"),
        "parts": sum(c.parts for c in normal),
        "threshold_s": group.threshold,
        "threshold_auto": group.fixed is None,
        **_summary(group.splits),
        "pauses": sum(1 for s in group.splits if s.pause_s > 0),
        "pause_s": sum(s.pause_s for s in group.splits),
        "norm_per_part_s": group.norms.lookup(program, end),
        "machine_per_part_median_s": _median([c.machine_s / c.parts for c in normal]),
        "vs_norm": round(statistics.median(ratios), 3) if ratios else None,
    }


async def summaries(
    session: AsyncSession, machines: Sequence[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Per machine and program: counts, medians and the norm, without histograms and breakdowns.

    The light part of ``stats``: cheap enough for every machine over 30 days (the overview).
    """
    return [_base(group, end) for group in await _groups(session, machines, start, end)]


async def stats(
    session: AsyncSession, machines: Sequence[dict[str, Any]], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Per machine and program: counts, medians, histograms, interruptions, the norm; by shift and by day."""
    result = []
    shifts_of: dict[int, list[Any]] = {}
    for group in await _groups(session, machines, start, end):
        machine, splits = group.machine, group.splits
        if machine["id"] not in shifts_of:
            shifts_of[machine["id"]] = await oee.site_shifts(session, machine, start, end)
        zone = ZoneInfo(machine["timezone"])
        by_shift, by_day = [], defaultdict(list)
        for shift in shifts_of[machine["id"]]:
            inside = [s for s in splits if shift.start <= s.cycle.end < shift.end]
            if inside:
                by_shift.append({"name": shift.name, "start": shift.start, "end": shift.end, **_summary(inside)})
        for s in splits:
            by_day[s.cycle.end.astimezone(zone).date().isoformat()].append(s)
        result.append(
            {
                **_base(group, end),
                "machine_hist": histogram([c.machine_s for c in group.normal]),
                "load_hist": histogram([s.load_s for s in splits if s.pause_s == 0]),
                "by_shift": by_shift,
                "by_day": [{"day": day, **_summary(items)} for day, items in sorted(by_day.items())],
            }
        )
    return result


async def last_cycle(session: AsyncSession, machine: dict[str, Any]) -> dict[str, Any] | None:
    """The last cycle of a machine and its norm, for the operator screen."""
    row = await timeline.last_cycle(session, machine["id"])
    if row is None:
        return None
    per_part = NormTable(await admin.cycle_norms(session, machine)).lookup(row["program"], row["end_ts"])
    return {
        "end": row["end_ts"].isoformat(),
        "program": row["program"],
        "parts": row["parts"],
        "kind": row["kind"],
        "duration_s": (row["end_ts"] - row["start_ts"]).total_seconds(),
        "machine_s": row["machine_s"],
        "gap_s": row["gap_s"],
        "norm_s": per_part * row["parts"] if per_part else None,
    }
