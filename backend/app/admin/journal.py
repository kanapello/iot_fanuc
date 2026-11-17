"""User input bound to time: downtime annotations and scrap records.

Only operators' actions write these tables. An open annotation ends when the
machine runs again; that end is derived on read (view downtime_annotations_resolved).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.oee.dirty import mark_oee_dirty
from app.timeline.service import mark_dirty as mark_timeline_dirty
from app.timeline.service import setup_active

_ANNOTATION_SELECT = (
    "SELECT a.id, a.machine_id, a.start_ts, a.effective_end_ts AS end_ts, a.end_ts IS NULL AS open_ended, "
    "a.reason_id, a.comment, a.operator, a.created_at, "
    "r.name AS reason_name, r.code AS reason_code, r.category, r.color, p.name AS group_name "
    "FROM downtime_annotations_resolved a JOIN downtime_reasons r ON r.id = a.reason_id "
    "LEFT JOIN downtime_reasons p ON p.id = r.parent_id "
)


async def add_annotation(
    session: AsyncSession,
    machine_id: int,
    start_ts: datetime,
    end_ts: datetime | None,
    reason_id: int,
    comment: str | None,
    operator: str | None,
) -> int:
    """Attach a reason to a period of time. An open annotation lasts until the machine runs again."""
    await _check_reason(session, "downtime_reasons", reason_id, machine_id)
    # one reason per moment: a new annotation replaces the open one of the same stretch
    await session.execute(
        text("DELETE FROM downtime_annotations WHERE machine_id = :m AND end_ts IS NULL AND start_ts >= :start"),
        {"m": machine_id, "start": start_ts},
    )
    new_id = (
        await session.execute(
            text(
                "INSERT INTO downtime_annotations (machine_id, start_ts, end_ts, reason_id, comment, operator) "
                "VALUES (:m, :start, :end, :reason, :comment, :operator) RETURNING id"
            ),
            {
                "m": machine_id,
                "start": start_ts,
                "end": end_ts,
                "reason": reason_id,
                "comment": comment,
                "operator": operator,
            },
        )
    ).scalar_one()
    await mark_oee_dirty(session, {machine_id: start_ts})
    return int(new_id)


class ForeignReasonError(ValueError):
    """The reason does not exist in the organization of the machine."""


async def _check_reason(session: AsyncSession, table: str, reason_id: int, machine_id: int) -> None:
    found = await session.execute(
        text(
            f"SELECT 1 FROM {table} r JOIN machines m ON m.id = :m JOIN sites s ON s.id = m.site_id "
            "WHERE r.id = :reason AND r.org_id = s.org_id AND (r.site_id IS NULL OR r.site_id = m.site_id)"
        ),
        {"m": machine_id, "reason": reason_id},
    )
    if found.first() is None:
        raise ForeignReasonError(reason_id)


async def delete_annotation(session: AsyncSession, annotation_id: int, machine_ids: Collection[int]) -> bool:
    """Delete an annotation of one of ``machine_ids`` (the caller's access scope)."""
    row = (
        await session.execute(
            text(
                "DELETE FROM downtime_annotations WHERE id = :id AND machine_id = ANY(:ids) "
                "RETURNING machine_id, start_ts"
            ),
            {"id": annotation_id, "ids": list(machine_ids)},
        )
    ).first()
    if row is None:
        return False
    await mark_oee_dirty(session, {row.machine_id: row.start_ts})
    return True


async def annotation_machine(session: AsyncSession, annotation_id: int) -> int | None:
    result = await session.execute(
        text("SELECT machine_id FROM downtime_annotations WHERE id = :id"), {"id": annotation_id}
    )
    return result.scalar_one_or_none()


async def list_annotations(
    session: AsyncSession, machine_ids: Collection[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            _ANNOTATION_SELECT + "WHERE a.machine_id = ANY(:ids) AND a.start_ts < :end "
            "AND (a.effective_end_ts IS NULL OR a.effective_end_ts > :start) ORDER BY a.machine_id, a.start_ts"
        ),
        {"ids": list(machine_ids), "start": start, "end": end},
    )
    return [dict(r._mapping) for r in rows]


async def current_reason(
    session: AsyncSession, machine_id: int, since: datetime, now: datetime
) -> dict[str, Any] | None:
    """Reason covering the current downtime stretch, if the operator gave one."""
    row = (
        await session.execute(
            text(
                _ANNOTATION_SELECT + "WHERE a.machine_id = :m AND a.start_ts <= :now "
                "AND (a.effective_end_ts IS NULL OR a.effective_end_ts > :since) ORDER BY a.start_ts DESC LIMIT 1"
            ),
            {"m": machine_id, "since": since, "now": now},
        )
    ).first()
    if row is None:
        return None
    return {"id": row.reason_id, "name": row.reason_name, "color": row.color, "category": row.category}


async def planned_downtime(
    session: AsyncSession, machine_id: int, start: datetime, end: datetime, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Periods annotated with a reason of category 'planned' (excluded from planned production time)."""
    rows = await list_annotations(session, [machine_id], start, end)
    return [(r["start_ts"], r["end_ts"] or now) for r in rows if r["category"] == "planned"]


async def add_scrap(
    session: AsyncSession,
    machine_id: int,
    ts: datetime,
    qty: int,
    reason_id: int | None,
    program: str | None,
    operator: str | None,
) -> int:
    if reason_id is not None:
        await _check_reason(session, "scrap_reasons", reason_id, machine_id)
    new_id = (
        await session.execute(
            text(
                "INSERT INTO scrap_records (machine_id, ts, program, qty, reason_id, operator) "
                "VALUES (:m, :ts, :program, :qty, :reason, :operator) RETURNING id"
            ),
            {"m": machine_id, "ts": ts, "program": program, "qty": qty, "reason": reason_id, "operator": operator},
        )
    ).scalar_one()
    await mark_oee_dirty(session, {machine_id: ts})
    return int(new_id)


async def list_scrap(
    session: AsyncSession, machine_ids: Collection[int], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT s.id, s.machine_id, s.ts, s.program, s.qty, s.reason_id, r.name AS reason_name, s.operator "
            "FROM scrap_records s LEFT JOIN scrap_reasons r ON r.id = s.reason_id "
            "WHERE s.machine_id = ANY(:ids) AND s.ts >= :start AND s.ts < :end ORDER BY s.ts DESC"
        ),
        {"ids": list(machine_ids), "start": start, "end": end},
    )
    return [dict(r._mapping) for r in rows]


async def set_setup(session: AsyncSession, machine_id: int, active: bool, operator: str | None, now: datetime) -> None:
    """Operator's manual "setup" mark; a change re-derives the timeline from that moment."""
    params = {"m": machine_id, "now": now, "operator": operator}
    if await setup_active(session, machine_id) == active:
        return
    if active:
        await session.execute(
            text("INSERT INTO operator_setup_marks (machine_id, start_ts, operator) VALUES (:m, :now, :operator)"),
            params,
        )
    else:
        await session.execute(
            text("UPDATE operator_setup_marks SET end_ts = :now WHERE machine_id = :m AND end_ts IS NULL"), params
        )
    await mark_timeline_dirty(session, {machine_id: now})
