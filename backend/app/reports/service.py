"""Reports built from derived data."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_PARETO = """
WITH down AS (
    SELECT id, machine_id, state, GREATEST(start_ts, :start) AS s, LEAST(COALESCE(end_ts, :now), :end) AS e
    FROM state_intervals
    WHERE machine_id = ANY(:ids) AND state <> 'RUN' AND start_ts < :end AND COALESCE(end_ts, :now) > :start
), ann AS (
    SELECT id, machine_id, reason_id, start_ts AS s, COALESCE(effective_end_ts, :now) AS e
    FROM downtime_annotations_resolved
    WHERE machine_id = ANY(:ids) AND start_ts < :end AND COALESCE(effective_end_ts, :now) > :start
), hit AS (
    SELECT d.id AS down_id, d.state, a.reason_id, a.id AS ann_id,
           EXTRACT(EPOCH FROM LEAST(d.e, a.e) - GREATEST(d.s, a.s)) AS seconds
    FROM down d JOIN ann a ON a.machine_id = d.machine_id AND a.s < d.e AND a.e > d.s
), by_reason AS (
    SELECT reason_id, NULL::text AS state, sum(seconds) AS duration_s, count(DISTINCT ann_id) AS count
    FROM hit GROUP BY reason_id
), unexplained AS (
    SELECT NULL::integer AS reason_id, d.state,
           sum(EXTRACT(EPOCH FROM d.e - d.s) - COALESCE(h.seconds, 0)) AS duration_s,
           count(*) FILTER (WHERE h.down_id IS NULL) AS count
    FROM down d LEFT JOIN (SELECT down_id, sum(seconds) AS seconds FROM hit GROUP BY down_id) h ON h.down_id = d.id
    GROUP BY d.state
)
SELECT u.reason_id, u.state, u.duration_s, u.count, r.name, r.code, r.category, r.color, p.name AS group_name
FROM (SELECT * FROM by_reason UNION ALL SELECT * FROM unexplained) u
LEFT JOIN downtime_reasons r ON r.id = u.reason_id
LEFT JOIN downtime_reasons p ON p.id = r.parent_id
WHERE u.duration_s > 0.5
ORDER BY u.duration_s DESC
"""


async def downtime_pareto(
    session: AsyncSession, machine_ids: Collection[int], start: datetime, end: datetime, now: datetime
) -> list[dict[str, Any]]:
    """Downtime by reason, longest first. Time without a reason is reported per state."""
    rows = await session.execute(
        text(_PARETO), {"ids": list(machine_ids), "start": start, "end": min(end, now), "now": now}
    )
    return [
        {**m, "duration_s": float(m["duration_s"]), "count": int(m["count"])} for m in (dict(r._mapping) for r in rows)
    ]
