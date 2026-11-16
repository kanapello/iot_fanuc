"""OEE persistence: recompute oee_hourly / oee_shift and read aggregates."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import journal
from app.admin import service as admin
from app.oee.calc import OeeResult, aggregate, compute_oee
from app.oee.shifts import Shift, ShiftTemplate, clip, shift_instances
from app.timeline import service as timeline

CALC_VERSION = 1
_COLUMNS = (
    "machine_id, period_start, period_end, planned_time_s, run_time_s, idle_s, stop_s, alarm_s, setup_s, offline_s, "
    "total_parts, good_parts, scrap_parts, ideal_time_s, no_standard, availability, performance, quality, oee, "
    "calc_version"
)
_VALUES = ", ".join(f":{name.strip()}" for name in _COLUMNS.split(","))


async def site_shifts(session: AsyncSession, machine: dict[str, Any], start: datetime, end: datetime) -> list[Shift]:
    templates = [ShiftTemplate.from_row(r) for r in await admin.shift_templates(session, machine["site_id"])]
    return shift_instances(templates, machine["timezone"], start, end)


async def recompute(
    session: AsyncSession, machine: dict[str, Any], start: datetime, end: datetime, now: datetime
) -> None:
    """Recompute every hourly and shift bucket overlapping [start, end]."""
    machine_id = machine["id"]
    end = max(end, start + timedelta(seconds=1))
    hour0 = start.replace(minute=0, second=0, microsecond=0)
    shifts = [s for s in await site_shifts(session, machine, start, end) if s.start <= now]
    lo = min([hour0, *(s.start for s in shifts)])
    hi = max([end, *(s.end for s in shifts)])
    all_shifts = await site_shifts(session, machine, lo, hi)

    intervals = [
        (i["state"], i["start_ts"], i["end_ts"]) for i in await timeline.get_intervals(session, [machine_id], lo, hi)
    ]
    parts = [(p["ts"], p["program"], p["delta"]) for p in await timeline.get_part_events(session, machine_id, lo, hi)]
    planned = await journal.planned_downtime(session, machine_id, lo, hi, now)
    scrap = [(s["ts"], s["qty"]) for s in await journal.list_scrap(session, [machine_id], lo, hi)]
    norms = await admin.cycle_norms(session, machine_id)

    def calc(windows: list[tuple[datetime, datetime]], p0: datetime, p1: datetime) -> OeeResult:
        scrapped = sum(qty for ts, qty in scrap if p0 <= ts < p1)
        return compute_oee(windows, intervals, parts, norms, scrapped, now, planned)

    def row(result: OeeResult, p0: datetime, p1: datetime) -> dict[str, Any]:
        return {
            **result.as_dict(),
            "machine_id": machine_id,
            "period_start": p0,
            "period_end": p1,
            "calc_version": CALC_VERSION,
        }

    hourly: list[dict[str, Any]] = []
    hour = hour0
    while hour < min(end, now):
        next_hour = hour + timedelta(hours=1)
        windows = clip([w for s in all_shifts for w in s.windows], hour, next_hour)
        if windows:
            hourly.append(row(calc(windows, hour, next_hour), hour, next_hour))
        hour = next_hour

    shift_rows = [
        {
            **row(calc(s.windows, s.start, s.end), s.start, s.end),
            "shift_name": s.name,
            "planned_total_s": sum((e - b).total_seconds() for b, e in s.windows),
        }
        for s in shifts
    ]

    params = {"m": machine_id, "lo": hour0, "hi": end}
    await session.execute(
        text("DELETE FROM oee_hourly WHERE machine_id = :m AND period_start >= :lo AND period_start < :hi"), params
    )
    if hourly:
        await session.execute(text(f"INSERT INTO oee_hourly ({_COLUMNS}) VALUES ({_VALUES})"), hourly)
    await session.execute(
        text("DELETE FROM oee_shift WHERE machine_id = :m AND period_end > :lo AND period_start < :hi"),
        {"m": machine_id, "lo": start, "hi": end},
    )
    if shift_rows:
        sql = f"INSERT INTO oee_shift ({_COLUMNS}, shift_name, planned_total_s) "
        sql += f"VALUES ({_VALUES}, :shift_name, :planned_total_s)"
        await session.execute(text(sql), shift_rows)


async def current_shift(session: AsyncSession, machine_id: str, now: datetime) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT * FROM oee_shift WHERE machine_id = :m AND period_start <= :now AND period_end > :now "
                "ORDER BY period_start DESC LIMIT 1"
            ),
            {"m": machine_id, "now": now},
        )
    ).first()
    return dict(row._mapping) if row else None


async def query(
    session: AsyncSession,
    machines: list[dict[str, Any]],
    period: str,
    start: datetime,
    end: datetime,
    group_machines: bool = False,
) -> list[dict[str, Any]]:
    """OEE rows for machines. period: hour | shift | day | total.

    ``group_machines`` merges all machines into one row per bucket (area view).
    """
    if not machines:
        return []
    table = "oee_shift" if period == "shift" else "oee_hourly"
    rows = [
        dict(r._mapping)
        for r in await session.execute(
            text(
                f"SELECT * FROM {table} WHERE machine_id = ANY(:ids) "
                "AND period_start >= :start AND period_start < :end ORDER BY period_start, machine_id"
            ),
            {"ids": [m["id"] for m in machines], "start": start, "end": end},
        )
    ]
    tz = ZoneInfo(machines[0]["timezone"])
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if period == "day":
            day = r["period_start"].astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            bucket: tuple[Any, ...] = (day, day + timedelta(days=1), day.strftime("%d.%m"))
        elif period == "total":
            bucket = (start, end, "")
        else:
            label = r.get("shift_name") or r["period_start"].astimezone(tz).strftime("%H:%M")
            bucket = (r["period_start"], r["period_end"], label)
        groups[(None if group_machines else r["machine_id"], *bucket)].append(r)

    result = []
    for (machine_id, p0, p1, label), items in groups.items():
        result.append(
            {"machine_id": machine_id, "period_start": p0, "period_end": p1, "label": label, **aggregate(items)}
        )
    return sorted(result, key=lambda r: (r["period_start"], r["machine_id"] or ""))
