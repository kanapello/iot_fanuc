"""Service functions of the admin module (reference data).

Other modules use these functions instead of touching the tables directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import Collector, Machine, Site


@dataclass(frozen=True, slots=True)
class MachineRef:
    site_id: str
    machine_id: str
    collector_id: str


async def register_discovered(session: AsyncSession, machines: Iterable[MachineRef]) -> None:
    """Auto-register sites, collectors and machines first seen in incoming data.

    Existing rows are never modified: names and settings edited by an
    administrator must survive.
    """
    refs = sorted(set(machines), key=lambda r: (r.site_id, r.collector_id, r.machine_id))
    if not refs:
        return
    await register_collectors(session, {(r.site_id, r.collector_id) for r in refs})
    await session.execute(
        insert(Machine)
        .values(
            [
                {"id": r.machine_id, "site_id": r.site_id, "name": r.machine_id, "collector_id": r.collector_id}
                for r in refs
            ]
        )
        .on_conflict_do_nothing(index_elements=[Machine.id])
    )


async def register_collectors(session: AsyncSession, collectors: set[tuple[str, str]]) -> None:
    """Make sure the (site_id, collector_id) pairs exist."""
    if not collectors:
        return
    sites = sorted({site for site, _ in collectors})
    await session.execute(
        insert(Site).values([{"id": s, "name": s} for s in sites]).on_conflict_do_nothing(index_elements=[Site.id])
    )
    await session.execute(
        insert(Collector)
        .values([{"id": c, "site_id": s, "name": c} for s, c in sorted(collectors)])
        .on_conflict_do_nothing(index_elements=[Collector.id])
    )


async def record_heartbeat(
    session: AsyncSession, site_id: str, collector_id: str, ts: datetime, version: str | None
) -> None:
    """Store the latest heartbeat; an older (redelivered) heartbeat never overwrites a newer one."""
    await register_collectors(session, {(site_id, collector_id)})
    stmt = insert(Collector).values(
        id=collector_id, site_id=site_id, name=collector_id, version=version, last_heartbeat=ts
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Collector.id],
            set_={"version": stmt.excluded.version, "last_heartbeat": stmt.excluded.last_heartbeat},
            where=or_(Collector.last_heartbeat.is_(None), Collector.last_heartbeat < stmt.excluded.last_heartbeat),
        )
    )
