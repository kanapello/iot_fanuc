"""Automatic recalculation marks after a change of reference data.

Derived data depends on shifts, cycle norms, state rules and downtime reasons.
Whoever changes them through the API only leaves a mark; the worker does the work.
A row of an organization (site_id NULL) may affect every site of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.oee.dirty import mark_oee_dirty
from app.timeline.service import mark_dirty as mark_timeline_dirty

_OEE_RESOURCES = ("programs", "shift-templates", "downtime-reasons")


async def _affected_machines(session: AsyncSession, versions: list[dict[str, Any]]) -> list[int]:
    ids: set[int] = set()
    for row in versions:
        found = await session.execute(
            text(
                "SELECT m.id FROM machines m JOIN sites s ON s.id = m.site_id WHERE m.is_active AND s.org_id = :org "
                "AND (CAST(:site AS integer) IS NULL OR m.site_id = :site) "
                "AND (CAST(:machine AS bigint) IS NULL OR m.id = :machine)"
            ),
            {"org": row["org_id"], "site": row.get("site_id"), "machine": row.get("machine_id")},
        )
        ids.update(r.id for r in found)
    return sorted(ids)


async def after_reference_change(
    session: AsyncSession,
    resource: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    now: datetime,
    horizon_days: int,
) -> int:
    """Mark what must be recomputed. Returns the number of machines marked."""
    versions = [row for row in (before, after) if row is not None]
    if not versions or resource not in (*_OEE_RESOURCES, "state-rules"):
        return 0
    horizon = now - timedelta(days=horizon_days)
    ids = await _affected_machines(session, versions)
    if resource == "state-rules":
        # the timeline itself changes; OEE follows it
        await mark_timeline_dirty(session, {m: horizon for m in ids})
    else:
        # a norm affects parts made from its valid_from on
        start = max(horizon, min(r["valid_from"] for r in versions)) if resource == "programs" else horizon
        await mark_oee_dirty(session, {m: start for m in ids})
    return len(ids)
