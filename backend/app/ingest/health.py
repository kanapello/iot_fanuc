"""Health of raw data delivery: gaps in per-machine sequence numbers."""

from __future__ import annotations

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
    WHERE ts >= :since AND (CAST(:machine_id AS text) IS NULL OR machine_id = CAST(:machine_id AS text))
    UNION ALL
    SELECT machine_id, collector_id, seq, ts, received_at FROM machine_events
    WHERE ts >= :since AND (CAST(:machine_id AS text) IS NULL OR machine_id = CAST(:machine_id AS text))
), ordered AS (
    SELECT *, lag(seq) OVER (PARTITION BY machine_id, collector_id ORDER BY seq) AS prev_seq,
           lag(ts) OVER (PARTITION BY machine_id, collector_id ORDER BY seq) AS prev_ts
    FROM raw
)
SELECT machine_id, collector_id, prev_seq AS after_seq, seq AS before_seq, seq - prev_seq - 1 AS missing,
       prev_ts AS after_ts, ts AS before_ts
FROM ordered
WHERE seq - prev_seq > 1 AND received_at < :settled
ORDER BY before_ts DESC
LIMIT 200
"""


async def find_seq_gaps(
    session: AsyncSession, now: datetime, window_hours: int = 24, machine_id: str | None = None
) -> list[dict[str, Any]]:
    """Gaps in ``seq`` among messages read on the edge during the last ``window_hours``.

    Computed from raw data on every call: a hole filled by late delivery
    disappears by itself, nothing has to be reset.
    """
    rows = await session.execute(
        text(_GAPS),
        {
            "since": now - timedelta(hours=window_hours),
            "settled": now - timedelta(seconds=GRACE_S),
            "machine_id": machine_id,
        },
    )
    return [
        {**m, "after_ts": m["after_ts"].isoformat(), "before_ts": m["before_ts"].isoformat()}
        for m in (dict(r._mapping) for r in rows)
    ]
