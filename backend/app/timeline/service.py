"""Timeline persistence: dirty marks, rebuild of state_intervals / part_events, queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.timeline.builder import IDLE, NOT_RUN, STOP, Interval, Rules, Sample, build_intervals, part_events

_SAMPLE_COLUMNS = "ts, connected, mode, run, alarm, emergency, program, part_count"


async def mark_dirty(session: AsyncSession, machines: Mapping[str, datetime]) -> None:
    """Tell the worker that data of ``machine`` changed at or after the given time."""
    if not machines:
        return
    await session.execute(
        text(
            "INSERT INTO timeline_dirty (machine_id, from_ts) VALUES (:machine_id, :from_ts) "
            "ON CONFLICT (machine_id) DO UPDATE SET from_ts = LEAST(timeline_dirty.from_ts, EXCLUDED.from_ts)"
        ),
        [{"machine_id": m, "from_ts": ts} for m, ts in sorted(machines.items())],
    )


async def load_rules(session: AsyncSession, machine_id: str) -> Rules:
    row = (
        await session.execute(
            text(
                "SELECT r.rules, r.version FROM state_rules r "
                "LEFT JOIN machines m ON m.id = :m "
                "WHERE r.id = COALESCE(m.state_rules_id, 1) ORDER BY r.id LIMIT 1"
            ),
            {"m": machine_id},
        )
    ).first()
    return Rules.from_config(row.rules, row.version) if row else Rules()


def _sample(row: Any) -> Sample:
    return Sample(
        ts=row.ts,
        connected=row.connected,
        mode=row.mode,
        run=row.run,
        alarm=row.alarm,
        emergency=row.emergency,
        program=row.program,
        part_count=row.part_count,
    )


async def rebuild(
    session: AsyncSession, machine_id: str, now: datetime, force_from: datetime | None = None
) -> datetime | None:
    """Bring the timeline of one machine up to ``now``.

    Rebuilds from the earliest changed moment (dirty mark or ``force_from``);
    with no new data only the tail is extended (OFFLINE / IDLE->STOP depend on
    time). Returns the moment from which derived data changed, if any.
    """
    params: dict[str, Any] = {"m": machine_id}
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:m))"), params)
    dirty = (
        await session.execute(text("DELETE FROM timeline_dirty WHERE machine_id = :m RETURNING from_ts"), params)
    ).scalar_one_or_none()
    changed_from = min((t for t in (dirty, force_from) if t is not None), default=None)
    start = min(changed_from, now) if changed_from is not None else now
    params["w"], params["now"] = start, now
    rules = await load_rules(session, machine_id)

    container = (
        await session.execute(
            text(
                "SELECT state, start_ts, end_ts, program FROM state_intervals "
                "WHERE machine_id = :m AND start_ts <= :w ORDER BY start_ts DESC LIMIT 1"
            ),
            params,
        )
    ).first()
    carry: Interval | None = None
    if container is not None:
        if container.state == STOP:
            # the stretch began idle_to_stop before the STOP interval (its IDLE part)
            stretch = container.start_ts - timedelta(seconds=rules.idle_to_stop_s)
            carry = Interval(NOT_RUN, stretch, None, container.program)
        else:
            state = NOT_RUN if container.state == IDLE else container.state
            carry = Interval(state, container.start_ts, None, container.program)

    prev_row = (
        await session.execute(
            text(f"SELECT {_SAMPLE_COLUMNS} FROM telemetry WHERE machine_id = :m AND ts < :w ORDER BY ts DESC LIMIT 1"),
            params,
        )
    ).first()
    rows = (
        await session.execute(
            text(
                f"SELECT {_SAMPLE_COLUMNS} FROM telemetry WHERE machine_id = :m AND ts >= :w AND ts <= :now ORDER BY ts"
            ),
            params,
        )
    ).all()
    samples = [_sample(r) for r in rows]
    if changed_from is None and not samples and container is not None and container.state == "OFFLINE":
        return None  # nothing can change until new data arrives

    marks = [
        (r.start_ts, r.end_ts)
        for r in await session.execute(
            text(
                "SELECT start_ts, end_ts FROM operator_setup_marks "
                "WHERE machine_id = :m AND (end_ts IS NULL OR end_ts > :w)"
            ),
            params,
        )
    ]
    intervals = build_intervals(
        samples, rules, start, now, prev=_sample(prev_row) if prev_row else None, carry=carry, marks=marks
    )
    if intervals:
        first = {"m": machine_id, "first": intervals[0].start}
        await session.execute(text("DELETE FROM state_intervals WHERE machine_id = :m AND start_ts >= :first"), first)
        await session.execute(
            text(
                "UPDATE state_intervals SET end_ts = :first "
                "WHERE machine_id = :m AND (end_ts IS NULL OR end_ts > :first)"
            ),
            first,
        )
        await session.execute(
            text(
                "INSERT INTO state_intervals (machine_id, state, start_ts, end_ts, program, rules_version) "
                "VALUES (:m, :state, :start_ts, :end_ts, :program, :version)"
            ),
            [
                {
                    "m": machine_id,
                    "state": i.state,
                    "start_ts": i.start,
                    "end_ts": i.end,
                    "program": i.program,
                    "version": rules.version,
                }
                for i in intervals
            ],
        )

    if changed_from is not None:
        prev_count = (
            await session.execute(
                text(
                    "SELECT part_count FROM telemetry WHERE machine_id = :m AND ts < :w "
                    "AND part_count IS NOT NULL ORDER BY ts DESC LIMIT 1"
                ),
                params,
            )
        ).scalar_one_or_none()
        await session.execute(text("DELETE FROM part_events WHERE machine_id = :m AND ts >= :w"), params)
        events = part_events(samples, prev_count)
        if events:
            await session.execute(
                text("INSERT INTO part_events (machine_id, ts, program, delta) VALUES (:m, :ts, :program, :delta)"),
                [{"m": machine_id, "ts": e.ts, "program": e.program, "delta": e.delta} for e in events],
            )
    return changed_from


async def get_intervals(
    session: AsyncSession, machine_ids: Sequence[str], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Intervals overlapping [start, end), oldest first."""
    rows = await session.execute(
        text(
            "SELECT machine_id, state, start_ts, end_ts, program FROM state_intervals "
            "WHERE machine_id = ANY(:ids) AND start_ts < :end AND (end_ts IS NULL OR end_ts > :start) "
            "ORDER BY machine_id, start_ts"
        ),
        {"ids": list(machine_ids), "start": start, "end": end},
    )
    return [dict(r._mapping) for r in rows]


async def get_part_events(
    session: AsyncSession, machine_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT ts, program, delta FROM part_events "
            "WHERE machine_id = :m AND ts >= :start AND ts < :end ORDER BY ts"
        ),
        {"m": machine_id, "start": start, "end": end},
    )
    return [dict(r._mapping) for r in rows]


async def current_state(session: AsyncSession, machine_id: str) -> dict[str, Any] | None:
    """Open interval plus the start of the whole non-running stretch it belongs to."""
    rows = (
        await session.execute(
            text(
                "SELECT state, start_ts, end_ts, program FROM state_intervals "
                "WHERE machine_id = :m ORDER BY start_ts DESC LIMIT 2"
            ),
            {"m": machine_id},
        )
    ).all()
    if not rows:
        return None
    current = rows[0]
    downtime_since = None
    if current.state != "RUN":
        downtime_since = current.start_ts
        if current.state == STOP and len(rows) > 1 and rows[1].state == IDLE and rows[1].end_ts == current.start_ts:
            downtime_since = rows[1].start_ts
    return {
        "state": current.state,
        "since": current.start_ts,
        "program": current.program,
        "downtime_since": downtime_since,
    }


async def latest_sample(session: AsyncSession, machine_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT ts, connected, mode, run, program, program_name, feed_actual, spindle_speed, spindle_load, "
                "part_count FROM telemetry WHERE machine_id = :m ORDER BY ts DESC LIMIT 1"
            ),
            {"m": machine_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def set_setup(session: AsyncSession, machine_id: str, active: bool, operator: str | None, now: datetime) -> None:
    """Operator's manual "setup" mark; a change re-derives the timeline from that moment."""
    params = {"m": machine_id, "now": now, "operator": operator}
    if await setup_active(session, machine_id) == active:
        return
    if active:
        await session.execute(
            text("INSERT INTO operator_setup_marks (machine_id, start_ts, operator) VALUES (:m, :now, :operator)"),
            params,
        )
    else:
        await session.execute(
            text("UPDATE operator_setup_marks SET end_ts = :now WHERE machine_id = :m AND end_ts IS NULL"), params
        )
    await mark_dirty(session, {machine_id: now})


async def setup_active(session: AsyncSession, machine_id: str) -> bool:
    result = await session.execute(
        text("SELECT 1 FROM operator_setup_marks WHERE machine_id = :m AND end_ts IS NULL LIMIT 1"), {"m": machine_id}
    )
    return result.first() is not None
