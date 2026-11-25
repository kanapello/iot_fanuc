"""Timeline persistence: dirty marks, rebuild of state_intervals / part_events, queries."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.timeline.builder import (
    IDLE,
    NOT_RUN,
    RUN,
    STOP,
    Interval,
    PartEvent,
    Rules,
    Sample,
    build_intervals,
    part_events,
)
from app.timeline.cycles import build_cycles

_SAMPLE_COLUMNS = "ts, connected, mode, run, alarm, emergency, program, part_count"


async def mark_dirty(session: AsyncSession, machines: Mapping[int, datetime]) -> None:
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


async def load_rules(session: AsyncSession, machine_id: int) -> Rules:
    """Rules of a machine: its own, else of its site, else the organization default, else built-in."""
    row = (
        await session.execute(
            text(
                "SELECT r.rules, r.version FROM machines m JOIN sites s ON s.id = m.site_id "
                "JOIN state_rules r ON r.id = m.state_rules_id "
                "OR (r.org_id = s.org_id AND (r.site_id = m.site_id OR r.site_id IS NULL)) "
                "WHERE m.id = :m "
                "ORDER BY (r.id = m.state_rules_id) DESC NULLS LAST, (r.site_id IS NOT NULL) DESC, r.id LIMIT 1"
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


CHUNK = timedelta(days=1)


async def rebuild(
    session: AsyncSession,
    machine_id: int,
    now: datetime,
    force_from: datetime | None = None,
    force_to: datetime | None = None,
) -> datetime | None:
    """Bring the timeline of one machine up to date.

    Without ``force_from`` the pending dirty mark is taken; with no new data
    only the tail is extended (OFFLINE / IDLE->STOP depend on time).

    With ``force_to`` only the closed period [force_from, force_to] is rebuilt
    and stitched to the untouched intervals after it. Long open periods are
    processed one CHUNK at a time: the rest stays marked dirty for the next call.

    Returns the moment from which derived data changed, if any.
    """
    params: dict[str, Any] = {"m": machine_id}
    await session.execute(text("SELECT pg_advisory_xact_lock(:m)"), params)
    changed_from = force_from
    if force_from is None:
        changed_from = (
            await session.execute(text("DELETE FROM timeline_dirty WHERE machine_id = :m RETURNING from_ts"), params)
        ).scalar_one_or_none()
    start = min(changed_from, now) if changed_from is not None else now
    rules = await load_rules(session, machine_id)

    # A closed rebuild looks a little past its end so that debounce and offline
    # decisions near the border are the same as in a full rebuild.
    lookahead = timedelta(seconds=2 * max(rules.offline_timeout_s, rules.min_interval_s))
    closed_to = force_to
    continue_from: datetime | None = None
    if closed_to is None and now - start > CHUNK:
        closed_to = continue_from = start + CHUNK
    if closed_to is not None and (closed_to + lookahead >= now or closed_to <= start):
        closed_to = None
    build_end = now if closed_to is None else closed_to + lookahead
    params["w"], params["end"] = start, build_end

    container = (
        await session.execute(
            text(
                "SELECT state, start_ts, end_ts, program FROM state_intervals "
                "WHERE machine_id = :m AND start_ts < :w ORDER BY start_ts DESC LIMIT 1"
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
                f"SELECT {_SAMPLE_COLUMNS} FROM telemetry WHERE machine_id = :m AND ts >= :w AND ts <= :end ORDER BY ts"
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
        samples, rules, start, build_end, prev=_sample(prev_row) if prev_row else None, carry=carry, marks=marks
    )
    if closed_to is not None:
        intervals = [i for i in intervals if i.start < closed_to]
        if intervals:
            intervals[-1] = Interval(intervals[-1].state, intervals[-1].start, closed_to, intervals[-1].program)
    if intervals:
        await _store_intervals(session, machine_id, intervals, closed_to, rules.version)

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
        parts_end = closed_to or build_end + timedelta(days=36500)
        await session.execute(
            text("DELETE FROM part_events WHERE machine_id = :m AND ts >= :w AND ts < :parts_end"),
            {**params, "parts_end": parts_end},
        )
        events = [e for e in part_events(samples, prev_count) if e.ts < parts_end]
        if events:
            await session.execute(
                text("INSERT INTO part_events (machine_id, ts, program, delta) VALUES (:m, :ts, :program, :delta)"),
                [{"m": machine_id, "ts": e.ts, "program": e.program, "delta": e.delta} for e in events],
            )
        await _rebuild_cycles(session, machine_id, start, parts_end, now)
    if continue_from is not None and closed_to is not None:
        await mark_dirty(session, {machine_id: continue_from})
    return changed_from


async def _part_events(session: AsyncSession, sql: str, params: dict[str, Any]) -> list[PartEvent]:
    rows = await session.execute(
        text(f"SELECT ts, program, delta FROM part_events WHERE machine_id = :m {sql}"), params
    )
    return [PartEvent(r.ts, r.program, r.delta) for r in rows]


async def _rebuild_cycles(
    session: AsyncSession, machine_id: int, since: datetime, until: datetime, now: datetime
) -> None:
    """Cycles whose window touches the rebuilt period [since, until), and the one crossing ``until``."""
    params: dict[str, Any] = {"m": machine_id, "since": since, "until": until}
    events = await _part_events(session, "AND ts >= :since AND ts < :until ORDER BY ts", params)
    events += await _part_events(session, "AND ts >= :until ORDER BY ts LIMIT 1", params)
    previous = await _part_events(session, "AND ts < :since ORDER BY ts DESC LIMIT 1", params)
    last = events[-1].ts if events else None
    await session.execute(
        text(
            "DELETE FROM cycles WHERE machine_id = :m AND end_ts >= :since "
            "AND (CAST(:last AS timestamptz) IS NULL OR end_ts <= :last)"
        ),
        {**params, "last": last},
    )
    if not events:
        return
    first = previous[0].ts if previous else events[0].ts
    rows = await session.execute(
        text(
            "SELECT state, start_ts, end_ts, program FROM state_intervals WHERE machine_id = :m "
            "AND start_ts < :hi AND (end_ts IS NULL OR end_ts > :lo) ORDER BY start_ts"
        ),
        {"m": machine_id, "lo": first, "hi": last},
    )
    intervals = [Interval(r.state, r.start_ts, r.end_ts, r.program) for r in rows]
    cycles = build_cycles(events, previous[0] if previous else None, intervals, now)
    if cycles:
        await session.execute(
            text(
                "INSERT INTO cycles (machine_id, end_ts, start_ts, program, parts, machine_s, gap_s, idle_s, stop_s, "
                "alarm_s, setup_s, offline_s, kind) VALUES (:m, :end, :start, :program, :parts, :machine, :gap, "
                ":idle, :stop, :alarm, :setup, :offline, :kind)"
            ),
            [
                {
                    "m": machine_id,
                    "end": c.end,
                    "start": c.start,
                    "program": c.program,
                    "parts": c.parts,
                    "machine": c.machine_s,
                    "gap": c.gap_s,
                    "idle": c.idle_s,
                    "stop": c.stop_s,
                    "alarm": c.alarm_s,
                    "setup": c.setup_s,
                    "offline": c.offline_s,
                    "kind": c.kind,
                }
                for c in cycles
            ],
        )


async def list_cycles(
    session: AsyncSession, machine_ids: Sequence[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Cycles whose part was done in [start, end), oldest first."""
    rows = await session.execute(
        text(
            "SELECT machine_id, start_ts, end_ts, program, parts, machine_s, gap_s, idle_s, stop_s, alarm_s, setup_s, "
            "offline_s, kind FROM cycles WHERE machine_id = ANY(:ids) AND end_ts >= :start AND end_ts < :end "
            "ORDER BY machine_id, end_ts"
        ),
        {"ids": list(machine_ids), "start": start, "end": end},
    )
    return [dict(r._mapping) for r in rows]


async def last_cycle(session: AsyncSession, machine_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT start_ts, end_ts, program, parts, machine_s, gap_s, idle_s, stop_s, alarm_s, setup_s, "
                "offline_s, kind FROM cycles WHERE machine_id = :m ORDER BY end_ts DESC LIMIT 1"
            ),
            {"m": machine_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def _store_intervals(
    session: AsyncSession, machine_id: int, intervals: list[Interval], closed_to: datetime | None, version: int
) -> None:
    """Replace the chain from the first new interval on; for a closed period keep what follows it."""
    params: dict[str, Any] = {"m": machine_id, "first": intervals[0].start, "to": closed_to}
    if closed_to is None:
        await session.execute(text("DELETE FROM state_intervals WHERE machine_id = :m AND start_ts >= :first"), params)
    else:
        await session.execute(
            text(
                "DELETE FROM state_intervals WHERE machine_id = :m AND start_ts >= :first "
                "AND end_ts IS NOT NULL AND end_ts <= :to"
            ),
            params,
        )
        # the old interval crossing the border keeps only its part after the border
        await session.execute(
            text(
                "UPDATE state_intervals SET start_ts = :to WHERE machine_id = :m AND start_ts >= :first "
                "AND start_ts < :to AND (end_ts IS NULL OR end_ts > :to)"
            ),
            params,
        )
    await session.execute(
        text(
            "UPDATE state_intervals SET end_ts = :first "
            "WHERE machine_id = :m AND start_ts < :first AND (end_ts IS NULL OR end_ts > :first)"
        ),
        params,
    )
    if closed_to is not None:
        tail = (
            await session.execute(
                text("SELECT id, state, end_ts FROM state_intervals WHERE machine_id = :m AND start_ts = :to"), params
            )
        ).first()
        last = intervals[-1]
        if tail is not None and tail.state == last.state:
            # same state on both sides of the border: one interval again
            await session.execute(text("DELETE FROM state_intervals WHERE id = :id"), {"id": tail.id})
            intervals = [*intervals[:-1], Interval(last.state, last.start, tail.end_ts, last.program)]
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
                "version": version,
            }
            for i in intervals
        ],
    )


async def get_intervals(
    session: AsyncSession, machine_ids: Sequence[int], start: datetime, end: datetime
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


async def first_run_at(session: AsyncSession, points: Sequence[tuple[int, datetime]]) -> list[datetime | None]:
    """For each (machine, moment): the first moment at or after it when the machine was in RUN.

    The moment itself if the machine was running then; None if it has not run since.
    """
    if not points:
        return []
    rows = await session.execute(
        text(
            "SELECT p.n, (SELECT GREATEST(i.start_ts, p.ts) FROM state_intervals i "
            "WHERE i.machine_id = p.machine_id AND i.state = :run AND (i.end_ts IS NULL OR i.end_ts > p.ts) "
            "ORDER BY i.start_ts LIMIT 1) AS run_at "
            "FROM unnest(CAST(:n AS integer[]), CAST(:machines AS bigint[]), CAST(:ts AS timestamptz[])) "
            "AS p(n, machine_id, ts)"
        ),
        {"n": list(range(len(points))), "machines": [m for m, _ in points], "ts": [t for _, t in points], "run": RUN},
    )
    found = {r.n: r.run_at for r in rows}
    return [found.get(n) for n in range(len(points))]


async def first_interval_at(session: AsyncSession, machine_ids: Collection[int]) -> dict[int, datetime]:
    """Where the timeline of each machine begins."""
    rows = await session.execute(
        text(
            "SELECT machine_id, min(start_ts) AS first FROM state_intervals WHERE machine_id = ANY(:ids) "
            "GROUP BY machine_id"
        ),
        {"ids": list(machine_ids)},
    )
    return {r.machine_id: r.first for r in rows}


async def run_seconds(session: AsyncSession, spans: Sequence[tuple[int, datetime, datetime]]) -> list[float]:
    """For each (machine, start, end): seconds the machine spent in RUN inside [start, end)."""
    if not spans:
        return []
    rows = await session.execute(
        text(
            "SELECT p.n, COALESCE(sum(EXTRACT(EPOCH FROM LEAST(COALESCE(i.end_ts, p.end_ts), p.end_ts) "
            "- GREATEST(i.start_ts, p.start_ts))), 0) AS seconds "
            "FROM unnest(CAST(:n AS integer[]), CAST(:machines AS bigint[]), CAST(:starts AS timestamptz[]), "
            "CAST(:ends AS timestamptz[])) AS p(n, machine_id, start_ts, end_ts) "
            "LEFT JOIN state_intervals i ON i.machine_id = p.machine_id AND i.state = :run "
            "AND i.start_ts < p.end_ts AND (i.end_ts IS NULL OR i.end_ts > p.start_ts) "
            "GROUP BY p.n"
        ),
        {
            "n": list(range(len(spans))),
            "machines": [m for m, _, _ in spans],
            "starts": [a for _, a, _ in spans],
            "ends": [b for _, _, b in spans],
            "run": RUN,
        },
    )
    found = {r.n: float(r.seconds) for r in rows}
    return [max(found.get(n, 0.0), 0.0) for n in range(len(spans))]


async def get_part_events(
    session: AsyncSession, machine_id: int, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT ts, program, delta FROM part_events "
            "WHERE machine_id = :m AND ts >= :start AND ts < :end ORDER BY ts"
        ),
        {"m": machine_id, "start": start, "end": end},
    )
    return [dict(r._mapping) for r in rows]


async def current_state(session: AsyncSession, machine_id: int) -> dict[str, Any] | None:
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


async def latest_sample(session: AsyncSession, machine_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT ts, connected, mode, run, alarm_code, program, program_name, feed_actual, spindle_speed, "
                "spindle_load, part_count FROM telemetry WHERE machine_id = :m ORDER BY ts DESC LIMIT 1"
            ),
            {"m": machine_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def setup_active(session: AsyncSession, machine_id: int) -> bool:
    result = await session.execute(
        text("SELECT 1 FROM operator_setup_marks WHERE machine_id = :m AND end_ts IS NULL LIMIT 1"), {"m": machine_id}
    )
    return result.first() is not None


Span = tuple[datetime, datetime]


def _span_params(spans: Sequence[Span]) -> dict[str, Any]:
    return {"n": list(range(len(spans))), "starts": [s for s, _ in spans], "ends": [e for _, e in spans]}


async def state_seconds(
    session: AsyncSession, machine_ids: Collection[int], spans: Sequence[Span], now: datetime
) -> dict[tuple[int, int], dict[str, float]]:
    """Seconds per state of each machine inside each span: {(span index, machine): {state: seconds}}.

    One query for any number of spans; an open interval lasts until ``now``.
    """
    if not machine_ids or not spans:
        return {}
    rows = await session.execute(
        text(
            "SELECT p.n, i.machine_id, i.state, sum(EXTRACT(EPOCH FROM LEAST(COALESCE(i.end_ts, :now), p.end_ts) "
            "- GREATEST(i.start_ts, p.start_ts))) AS seconds "
            "FROM unnest(CAST(:n AS integer[]), CAST(:starts AS timestamptz[]), CAST(:ends AS timestamptz[])) "
            "AS p(n, start_ts, end_ts) "
            "JOIN state_intervals i ON i.machine_id = ANY(:ids) AND i.start_ts < p.end_ts "
            "AND COALESCE(i.end_ts, :now) > p.start_ts "
            "GROUP BY p.n, i.machine_id, i.state"
        ),
        {**_span_params(spans), "ids": list(machine_ids), "now": now},
    )
    result: dict[tuple[int, int], dict[str, float]] = {}
    for r in rows:
        result.setdefault((r.n, r.machine_id), {})[r.state] = max(float(r.seconds), 0.0)
    return result


async def part_totals(
    session: AsyncSession, machine_ids: Collection[int], spans: Sequence[Span]
) -> dict[tuple[int, int], int]:
    """Parts made by each machine inside each span: {(span index, machine): parts}."""
    if not machine_ids or not spans:
        return {}
    rows = await session.execute(
        text(
            "SELECT p.n, e.machine_id, sum(e.delta) AS parts "
            "FROM unnest(CAST(:n AS integer[]), CAST(:starts AS timestamptz[]), CAST(:ends AS timestamptz[])) "
            "AS p(n, start_ts, end_ts) "
            "JOIN part_events e ON e.machine_id = ANY(:ids) AND e.ts >= p.start_ts AND e.ts < p.end_ts "
            "GROUP BY p.n, e.machine_id"
        ),
        {**_span_params(spans), "ids": list(machine_ids)},
    )
    return {(r.n, r.machine_id): int(r.parts) for r in rows}
