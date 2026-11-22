"""Alarm journal: alarm episodes from raw machine events, their frequency per machine and the telemetry before them.

An episode of alarm ``code`` on a machine starts with ``alarm_raised`` and ends with the next
``alarm_cleared`` of the same code. A repeated ``alarm_raised`` while the episode is open (the
collector announces active alarms again after a restart) does not start a new one. If the clearing
event was lost (the alarm went away while the collector was down), the episode ends at the first
telemetry sample of a connected machine without any alarm; with no such sample it is still active.

Everything here is read from raw data on request: nothing is stored, so recalculation is never needed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ALARM_TYPES = ("alarm_raised", "alarm_cleared")
# an alarm raised this long before the period may still re-announce itself inside it
LOOKBACK = timedelta(days=1)
CONTEXT_MINUTES = 10
TELEMETRY_FIELDS = (
    "ts, connected, mode, run, program, motion, alarm, emergency, feed_actual, spindle_speed, spindle_load, "
    "feed_override, spindle_override, part_count"
)


async def _alarm_events(session: AsyncSession, machine_ids: Sequence[int], start: datetime, end: datetime) -> list[Any]:
    rows = await session.execute(
        text(
            "SELECT machine_id, ts, type, code, message, payload->>'category' AS category "
            "FROM machine_events WHERE machine_id = ANY(:ids) AND type IN ('alarm_raised', 'alarm_cleared') "
            "AND ts >= :start AND ts < :end ORDER BY machine_id, ts"
        ),
        {"ids": list(machine_ids), "start": start, "end": end},
    )
    return list(rows)


def _pair(events: Sequence[Any]) -> list[dict[str, Any]]:
    """Episodes from the raised/cleared events of machines, oldest first per machine and code."""
    open_: dict[tuple[int, str | None], dict[str, Any]] = {}
    episodes: list[dict[str, Any]] = []
    for e in events:
        key = (e.machine_id, e.code)
        if e.type == "alarm_raised":
            if key not in open_:
                episode = {
                    "machine_id": e.machine_id,
                    "code": e.code,
                    "message": e.message,
                    "category": e.category,
                    "start_ts": e.ts,
                    "end_ts": None,
                }
                open_[key] = episode
                episodes.append(episode)
        elif key in open_:  # cleared; a clearing without a raise before it is ignored
            open_.pop(key)["end_ts"] = e.ts
    return episodes


async def _close_lost(session: AsyncSession, episodes: list[dict[str, Any]]) -> None:
    """End open episodes at the first sample of a connected machine without any alarm."""
    for episode in (e for e in episodes if e["end_ts"] is None):
        row = (
            await session.execute(
                text(
                    "SELECT min(ts) FROM telemetry WHERE machine_id = :m AND ts > :since "
                    "AND connected AND alarm IS FALSE"
                ),
                {"m": episode["machine_id"], "since": episode["start_ts"]},
            )
        ).scalar_one_or_none()
        episode["end_ts"] = row


async def episodes(
    session: AsyncSession,
    machine_ids: Sequence[int],
    start: datetime,
    end: datetime,
    now: datetime,
    code: str | None = None,
) -> list[dict[str, Any]]:
    """Alarm episodes that started in [start, end), newest first, with their duration.

    ``active`` episodes last until ``now``; events up to ``now`` are read so that an alarm raised
    inside the period and cleared after it gets its full duration.
    """
    if not machine_ids:
        return []
    found = _pair(await _alarm_events(session, machine_ids, start - LOOKBACK, now + timedelta(seconds=1)))
    found = [e for e in found if start <= e["start_ts"] < end and (code is None or e["code"] == code)]
    await _close_lost(session, found)
    for e in found:
        e["active"] = e["end_ts"] is None
        e["duration_s"] = ((e["end_ts"] or now) - e["start_ts"]).total_seconds()
    return sorted(found, key=lambda e: (e["start_ts"], e["machine_id"]), reverse=True)


async def find_episode(
    session: AsyncSession, machine_id: int, start_ts: datetime, code: str, now: datetime
) -> dict[str, Any] | None:
    """The episode of ``code`` that started exactly at ``start_ts`` (a repair request is opened from it)."""
    window_end = start_ts + timedelta(microseconds=1)
    return next(
        iter(await episodes(session, [machine_id], start_ts, window_end, now, code)),
        None,
    )


def frequency(found: Sequence[dict[str, Any]], machine_ids: Sequence[int], days: float) -> list[dict[str, Any]]:
    """Per machine: how many alarm episodes, how often per day, how long in total, and by alarm code."""
    by_machine: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in found:
        by_machine[e["machine_id"]].append(e)
    result = []
    for machine_id in machine_ids:
        own = by_machine.get(machine_id, [])
        codes: dict[str | None, dict[str, Any]] = {}
        for e in own:
            entry = codes.setdefault(
                e["code"], {"code": e["code"], "message": e["message"], "count": 0, "duration_s": 0.0}
            )
            entry["count"] += 1
            entry["duration_s"] += e["duration_s"]
        result.append(
            {
                "machine_id": machine_id,
                "count": len(own),
                "per_day": round(len(own) / days, 3) if days > 0 else None,
                "duration_s": sum(e["duration_s"] for e in own),
                "active": sum(1 for e in own if e["active"]),
                "codes": sorted(codes.values(), key=lambda c: (-c["count"], -c["duration_s"], str(c["code"]))),
            }
        )
    return sorted(result, key=lambda r: (-r["count"], -r["duration_s"], r["machine_id"]))


async def context(
    session: AsyncSession, machine_id: int, ts: datetime, minutes: int = CONTEXT_MINUTES
) -> dict[str, Any]:
    """Telemetry of the ``minutes`` before an alarm, with the machine events of the same window.

    The last sample before the window is included: telemetry is sent on change, so it is the state
    the window starts in.
    """
    window_start = ts - timedelta(minutes=minutes)
    before = (
        await session.execute(
            text(
                f"SELECT {TELEMETRY_FIELDS} FROM telemetry WHERE machine_id = :m AND ts < :start "
                "ORDER BY ts DESC LIMIT 1"
            ),
            {"m": machine_id, "start": window_start},
        )
    ).first()
    rows = await session.execute(
        text(
            f"SELECT {TELEMETRY_FIELDS} FROM telemetry WHERE machine_id = :m AND ts >= :start AND ts <= :ts ORDER BY ts"
        ),
        {"m": machine_id, "start": window_start, "ts": ts},
    )
    samples = ([dict(before._mapping)] if before else []) + [dict(r._mapping) for r in rows]
    events = await session.execute(
        text(
            "SELECT ts, type, code, message FROM machine_events WHERE machine_id = :m AND ts >= :start "
            "AND ts <= :ts ORDER BY ts"
        ),
        {"m": machine_id, "start": window_start, "ts": ts},
    )
    return {
        "machine_id": machine_id,
        "from": window_start,
        "to": ts,
        "telemetry": samples,
        "events": [dict(r._mapping) for r in events],
    }
