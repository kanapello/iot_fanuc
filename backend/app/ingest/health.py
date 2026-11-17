"""Health of raw data delivery: gaps in per-machine sequence numbers."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Messages of one machine may arrive out of order (buffer resend, batching),
# so a hole counts as a gap only after it stayed open for this long.
GRACE_S = 60.0

_GAPS = """
WITH raw AS (
    SELECT machine_id, collector_id, seq, ts, received_at FROM telemetry
    WHERE ts >= :since AND machine_id = ANY(:machine_ids)
    UNION ALL
    SELECT machine_id, collector_id, seq, ts, received_at FROM machine_events
    WHERE ts >= :since AND machine_id = ANY(:machine_ids)
), current_epoch AS (
    -- after a collector lost its buffer its counters restart: older messages belong to another numbering
    SELECT r.* FROM raw r LEFT JOIN collectors c ON c.id = r.collector_id
    WHERE c.buffer_lost_at IS NULL OR r.received_at >= c.buffer_lost_at
), ordered AS (
    SELECT *, lag(seq) OVER (PARTITION BY machine_id, collector_id ORDER BY seq) AS prev_seq,
           lag(ts) OVER (PARTITION BY machine_id, collector_id ORDER BY seq) AS prev_ts
    FROM current_epoch
)
SELECT machine_id, collector_id, prev_seq AS after_seq, seq AS before_seq, seq - prev_seq - 1 AS missing,
       prev_ts AS after_ts, ts AS before_ts
FROM ordered
WHERE seq - prev_seq > 1 AND received_at < :settled
ORDER BY before_ts DESC
LIMIT 200
"""


async def find_seq_gaps(
    session: AsyncSession, machine_ids: Collection[int], now: datetime, window_hours: int = 24
) -> list[dict[str, Any]]:
    """Gaps in ``seq`` of the given machines among messages read during the last ``window_hours``.

    Computed from raw data on every call: a hole filled by late delivery
    disappears by itself, nothing has to be reset.
    """
    rows = await session.execute(
        text(_GAPS),
        {
            "since": now - timedelta(hours=window_hours),
            "settled": now - timedelta(seconds=GRACE_S),
            "machine_ids": list(machine_ids),
        },
    )
    return [
        {**m, "after_ts": m["after_ts"].isoformat(), "before_ts": m["before_ts"].isoformat()}
        for m in (dict(r._mapping) for r in rows)
    ]


async def alarms_since(
    session: AsyncSession, since: dict[int, datetime]
) -> dict[int, list[tuple[datetime, str | None, str | None, str | None]]]:
    """Alarms raised on each machine after the given moment: (ts, code, category, message)."""
    result: dict[int, list[tuple[datetime, str | None, str | None, str | None]]] = {m: [] for m in since}
    if not since:
        return result
    rows = await session.execute(
        text(
            "SELECT e.machine_id, e.ts, e.code, e.payload->>'category' AS category, e.message "
            "FROM machine_events e JOIN unnest(CAST(:machines AS bigint[]), CAST(:since AS timestamptz[])) "
            "AS s(machine_id, since) ON s.machine_id = e.machine_id "
            "WHERE e.type = 'alarm_raised' AND e.ts > s.since ORDER BY e.ts"
        ),
        {"machines": list(since), "since": list(since.values())},
    )
    for r in rows:
        result[r.machine_id].append((r.ts, r.code, r.category, r.message))
    return result
