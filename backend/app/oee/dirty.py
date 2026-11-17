"""Persistent "recompute OEE from this moment" marks (table oee_dirty)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def mark_oee_dirty(session: AsyncSession, machines: Mapping[int, datetime]) -> None:
    """Remember that OEE of ``machine`` must be recomputed from the given time on."""
    if not machines:
        return
    await session.execute(
        text(
            "INSERT INTO oee_dirty (machine_id, from_ts) VALUES (:machine_id, :from_ts) "
            "ON CONFLICT (machine_id) DO UPDATE SET from_ts = LEAST(oee_dirty.from_ts, EXCLUDED.from_ts)"
        ),
        [{"machine_id": m, "from_ts": ts} for m, ts in sorted(machines.items())],
    )


async def take_oee_dirty(session: AsyncSession, machine_id: int) -> datetime | None:
    """Remove and return the mark; the caller recomputes in the same transaction."""
    result = await session.execute(
        text("DELETE FROM oee_dirty WHERE machine_id = :m RETURNING from_ts"), {"m": machine_id}
    )
    return result.scalar_one_or_none()
