"""Automatic recalculation marks after a change of reference data.

Derived data depends on shifts, cycle norms, state rules and downtime reasons.
Whoever changes them through the API only leaves a mark; the worker does the work.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.oee.dirty import mark_oee_dirty
from app.timeline.service import mark_dirty as mark_timeline_dirty


async def _machine_ids(session: AsyncSession, where: str = "true", **params: Any) -> list[str]:
    rows = await session.execute(text(f"SELECT id FROM machines WHERE is_active AND ({where})"), params)
    return [r.id for r in rows]


async def after_reference_change(
    session: AsyncSession,
    resource: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    now: datetime,
    horizon_days: int,
) -> int:
    """Mark what must be recomputed. Returns the number of machines marked."""
    horizon = now - timedelta(days=horizon_days)
    versions = [row for row in (before, after) if row is not None]
    if not versions:
        return 0

    if resource == "state-rules":
        # the timeline itself changes; OEE follows it
        ids = await _machine_ids(
            session, "COALESCE(state_rules_id, 1) = ANY(:rules)", rules=[r["id"] for r in versions]
        )
        await mark_timeline_dirty(session, {m: horizon for m in ids})
        return len(ids)

    if resource == "programs":
        # a norm affects parts made from its valid_from on
        start = max(horizon, min(r["valid_from"] for r in versions))
        machine_ids = {r["machine_id"] for r in versions}
        ids = (
            await _machine_ids(session)
            if None in machine_ids
            else await _machine_ids(session, "id = ANY(:ids)", ids=sorted(machine_ids))
        )
    elif resource == "shift-templates":
        start = horizon
        ids = await _machine_ids(session, "site_id = ANY(:sites)", sites=sorted({r["site_id"] for r in versions}))
    elif resource == "downtime-reasons":
        start = horizon  # the category (planned / unplanned) changes planned production time
        ids = await _machine_ids(session)
    else:
        return 0
    await mark_oee_dirty(session, {m: start for m in ids})
    return len(ids)
