"""Service functions of the admin module (reference data).

Other modules use these functions instead of touching the tables directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, text
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


async def list_machines(session: AsyncSession, active_only: bool = True) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT m.id, m.name, m.site_id, m.area_id, a.name AS area_name, m.controller_type, m.collector_id, "
            "m.is_active, m.sort_order, s.timezone "
            "FROM machines m JOIN sites s ON s.id = m.site_id LEFT JOIN areas a ON a.id = m.area_id "
            "WHERE m.is_active OR NOT :active_only ORDER BY m.sort_order, m.id"
        ),
        {"active_only": active_only},
    )
    return [dict(r._mapping) for r in rows]


async def get_machine(session: AsyncSession, machine_id: str) -> dict[str, Any] | None:
    return next((m for m in await list_machines(session, active_only=False) if m["id"] == machine_id), None)


async def shift_templates(session: AsyncSession, site_id: str) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT name, start_time, end_time, days_of_week, planned_breaks FROM shift_templates "
            "WHERE site_id = :site AND is_active ORDER BY start_time"
        ),
        {"site": site_id},
    )
    return [dict(r._mapping) for r in rows]


async def cycle_norms(session: AsyncSession, machine_id: str) -> list[tuple[str, datetime, float, bool]]:
    """Norm rows for a machine: (program, valid_from, seconds per part, machine-specific)."""
    rows = await session.execute(
        text(
            "SELECT program, valid_from, ideal_cycle_time_s / parts_per_cycle AS seconds_per_part, "
            "machine_id IS NOT NULL AS specific FROM programs WHERE machine_id = :m OR machine_id IS NULL"
        ),
        {"m": machine_id},
    )
    return [(r.program, r.valid_from, float(r.seconds_per_part), bool(r.specific)) for r in rows]


async def list_collectors(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT c.id, c.site_id, c.name, c.version, c.last_heartbeat, "
            "COALESCE(array_agg(m.id ORDER BY m.id) FILTER (WHERE m.id IS NOT NULL), '{}') AS machine_ids "
            "FROM collectors c LEFT JOIN machines m ON m.collector_id = c.id GROUP BY c.id ORDER BY c.id"
        )
    )
    return [dict(r._mapping) for r in rows]
