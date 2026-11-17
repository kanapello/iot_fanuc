"""Reference data: hierarchy (organization -> site -> area -> machine), collectors, effective references.

Functions that return customer data take the set of machine (or site) ids the
caller may reach as an explicit argument. ``*_system`` functions are for
background processes that act for the whole installation.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_MACHINE_SELECT = (
    "SELECT m.id, m.code, m.name, m.site_id, s.code AS site_code, s.name AS site_name, s.timezone, "
    "s.org_id, o.code AS org_code, o.name AS org_name, m.area_id, a.name AS area_name, m.controller_type, "
    "m.collector_id, m.is_active, m.sort_order, m.state_rules_id "
    "FROM machines m JOIN sites s ON s.id = m.site_id JOIN organizations o ON o.id = s.org_id "
    "LEFT JOIN areas a ON a.id = m.area_id "
)


async def list_machines(
    session: AsyncSession, machine_ids: Collection[int], active_only: bool = True
) -> list[dict[str, Any]]:
    """Machines among ``machine_ids`` (the caller's access scope)."""
    rows = await session.execute(
        text(
            _MACHINE_SELECT + "WHERE m.id = ANY(:ids) AND (m.is_active OR NOT :active_only) "
            "ORDER BY o.code, s.code, m.sort_order, m.code"
        ),
        {"ids": list(machine_ids), "active_only": active_only},
    )
    return [dict(r._mapping) for r in rows]


async def list_machines_system(session: AsyncSession, active_only: bool = True) -> list[dict[str, Any]]:
    """Every machine of the installation: for the worker and maintenance commands only."""
    rows = await session.execute(
        text(_MACHINE_SELECT + "WHERE m.is_active OR NOT :active_only ORDER BY m.id"), {"active_only": active_only}
    )
    return [dict(r._mapping) for r in rows]


async def hierarchy(session: AsyncSession) -> dict[str, list[dict[str, Any]]]:
    """Bare structure of the installation; the access layer filters it per principal."""
    orgs = await session.execute(text("SELECT id, code, name FROM organizations ORDER BY name"))
    sites = await session.execute(text("SELECT id, org_id, code, name, timezone FROM sites ORDER BY name"))
    areas = await session.execute(text("SELECT id, site_id, name FROM areas ORDER BY name"))
    machines = await session.execute(
        text("SELECT m.id, s.org_id, m.site_id, m.area_id FROM machines m JOIN sites s ON s.id = m.site_id")
    )
    return {
        "organizations": [dict(r._mapping) for r in orgs],
        "sites": [dict(r._mapping) for r in sites],
        "areas": [dict(r._mapping) for r in areas],
        "machines": [dict(r._mapping) for r in machines],
    }


# ---------------------------------------------------------------- effective references of a site
# A site uses its own rows of a reference if it has any, otherwise the organization defaults.

_EFFECTIVE = (
    "(t.org_id = :org AND (t.site_id = :site OR (t.site_id IS NULL AND NOT EXISTS ("
    "SELECT 1 FROM {table} own WHERE own.org_id = :org AND own.site_id = :site))))"
)


async def shift_templates(session: AsyncSession, org_id: int, site_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT t.name, t.start_time, t.end_time, t.days_of_week, t.planned_breaks FROM shift_templates t WHERE "
            + _EFFECTIVE.format(table="shift_templates")
            + " AND t.is_active ORDER BY t.start_time"
        ),
        {"org": org_id, "site": site_id},
    )
    return [dict(r._mapping) for r in rows]


async def downtime_reasons(session: AsyncSession, org_id: int, site_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT t.* FROM downtime_reasons t WHERE "
            + _EFFECTIVE.format(table="downtime_reasons")
            + " AND t.is_active ORDER BY t.sort_order, t.id"
        ),
        {"org": org_id, "site": site_id},
    )
    return [dict(r._mapping) for r in rows]


async def scrap_reasons(session: AsyncSession, org_id: int, site_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT t.* FROM scrap_reasons t WHERE "
            + _EFFECTIVE.format(table="scrap_reasons")
            + " AND t.is_active ORDER BY t.id"
        ),
        {"org": org_id, "site": site_id},
    )
    return [dict(r._mapping) for r in rows]


async def cycle_norms(session: AsyncSession, machine: dict[str, Any]) -> list[tuple[str, datetime, float, int]]:
    """Norm rows that may apply to a machine: (program, valid_from, seconds per part, rank).

    Rank: 2 = norm of this machine, 1 = of its site, 0 = organization default.
    """
    rows = await session.execute(
        text(
            "SELECT program, valid_from, ideal_cycle_time_s / parts_per_cycle AS seconds_per_part, "
            "CASE WHEN machine_id IS NOT NULL THEN 2 WHEN site_id IS NOT NULL THEN 1 ELSE 0 END AS rank "
            "FROM programs WHERE org_id = :org AND (machine_id = :m OR "
            "(machine_id IS NULL AND (site_id = :site OR site_id IS NULL)))"
        ),
        {"org": machine["org_id"], "site": machine["site_id"], "m": machine["id"]},
    )
    return [(r.program, r.valid_from, float(r.seconds_per_part), int(r.rank)) for r in rows]


# ---------------------------------------------------------------- collectors (used by ingest)


@dataclass(frozen=True, slots=True)
class CollectorRef:
    id: str
    site_id: int
    site_code: str
    org_code: str


async def collector_directory(session: AsyncSession) -> dict[str, CollectorRef]:
    rows = await session.execute(
        text(
            "SELECT c.id, c.site_id, s.code AS site_code, o.code AS org_code FROM collectors c "
            "JOIN sites s ON s.id = c.site_id JOIN organizations o ON o.id = s.org_id"
        )
    )
    return {r.id: CollectorRef(r.id, r.site_id, r.site_code, r.org_code) for r in rows}


async def register_machines(
    session: AsyncSession, machines: Iterable[tuple[int, str, str]]
) -> dict[tuple[int, str], int]:
    """Machines first seen in incoming data: (site_id, code, collector_id) -> machine id.

    A machine is created only inside the site of its collector. Existing rows
    are never modified: what an administrator edited must survive.
    """
    refs = sorted(set(machines))
    if not refs:
        return {}
    await session.execute(
        text(
            "INSERT INTO machines (site_id, code, name, collector_id) VALUES (:site_id, :code, :code, :collector_id) "
            "ON CONFLICT (site_id, code) DO NOTHING"
        ),
        [{"site_id": site_id, "code": code, "collector_id": collector} for site_id, code, collector in refs],
    )
    rows = await session.execute(
        text("SELECT id, site_id, code FROM machines WHERE site_id = ANY(:sites) AND code = ANY(:codes)"),
        {"sites": sorted({r[0] for r in refs}), "codes": sorted({r[1] for r in refs})},
    )
    return {(r.site_id, r.code): r.id for r in rows}


async def record_heartbeat(
    session: AsyncSession, collector_id: str, ts: datetime, version: str | None, epoch: str | None
) -> bool:
    """Store the latest heartbeat. Returns True if the collector reported a new buffer epoch.

    A new epoch means the collector lost its buffer: unsent data is gone and
    its ``seq`` counters started again from 1. An older (redelivered)
    heartbeat never overwrites a newer one.
    """
    row = (
        await session.execute(
            text("SELECT epoch, last_heartbeat FROM collectors WHERE id = :id FOR UPDATE"), {"id": collector_id}
        )
    ).first()
    if row is None or (row.last_heartbeat is not None and row.last_heartbeat >= ts):
        return False
    lost = row.epoch is not None and epoch is not None and row.epoch != epoch
    await session.execute(
        text(
            "UPDATE collectors SET last_heartbeat = :ts, version = :version, epoch = COALESCE(:epoch, epoch), "
            "buffer_lost_at = CASE WHEN :lost THEN :ts ELSE buffer_lost_at END WHERE id = :id"
        ),
        {"id": collector_id, "ts": ts, "version": version, "epoch": epoch, "lost": lost},
    )
    return lost


async def list_collectors(session: AsyncSession, site_ids: Collection[int]) -> list[dict[str, Any]]:
    """Collectors of the given sites (the caller's access scope)."""
    rows = await session.execute(
        text(
            "SELECT c.id, c.site_id, s.name AS site_name, c.name, c.version, c.last_heartbeat, c.epoch, "
            "c.buffer_lost_at, "
            "COALESCE(array_agg(m.id ORDER BY m.id) FILTER (WHERE m.id IS NOT NULL), '{}') AS machine_ids "
            "FROM collectors c JOIN sites s ON s.id = c.site_id LEFT JOIN machines m ON m.collector_id = c.id "
            "WHERE c.site_id = ANY(:sites) GROUP BY c.id, s.name ORDER BY c.id"
        ),
        {"sites": list(site_ids)},
    )
    return [dict(r._mapping) for r in rows]
