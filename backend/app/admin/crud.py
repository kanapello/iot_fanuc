"""CRUD over reference and structure tables, always inside an access scope.

Every row belongs to a node of the hierarchy (organization or site). Reading
and writing are checked against the caller's scope here, not in the routes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import NORMS_EDIT, REFERENCE_EDIT, REFERENCE_VIEW, STRUCTURE_EDIT, Scope

CODE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


class NotFoundError(Exception):
    """The object does not exist or lies outside the caller's scope (both look the same)."""


class ForbiddenError(Exception):
    """The object is visible to the caller but the action is not allowed."""


class InvalidReferenceError(ValueError):
    """A referenced row belongs to another node of the hierarchy."""


class OrganizationIn(BaseModel):
    code: str = Field(pattern=CODE)
    name: str


class SiteIn(BaseModel):
    org_id: int
    code: str = Field(pattern=CODE)
    name: str
    timezone: str = "Europe/Moscow"

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown time zone") from exc
        return value


class AreaIn(BaseModel):
    site_id: int
    name: str


class CollectorIn(BaseModel):
    id: str = Field(pattern=CODE)
    site_id: int
    name: str


class MachineIn(BaseModel):
    name: str
    area_id: int | None = None
    controller_type: str | None = None
    is_active: bool = True
    sort_order: int = 0
    # a gap from a part to the next start longer than this is downtime, not part change;
    # empty = three times the median machine time of the program, at least 10 minutes
    cycle_pause_threshold_s: float | None = Field(default=None, gt=0, le=86400)


class _Node(BaseModel):
    org_id: int
    site_id: int | None = None  # None = default for every site of the organization


class ShiftBreak(BaseModel):
    name: str = ""
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class ShiftTemplateIn(_Node):
    name: str
    start_time: time
    end_time: time
    days_of_week: list[int] = [1, 2, 3, 4, 5, 6, 7]
    planned_breaks: list[ShiftBreak] = []
    is_active: bool = True


class DowntimeReasonIn(_Node):
    parent_id: int | None = None
    code: str
    name: str
    category: str = Field(default="unplanned", pattern="^(planned|unplanned)$")
    color: str = "#64748b"
    requires_comment: bool = False
    sort_order: int = 0
    is_active: bool = True


class ScrapReasonIn(_Node):
    code: str
    name: str
    is_active: bool = True


class ProgramIn(_Node):
    machine_id: int | None = None
    program: str
    ideal_cycle_time_s: float = Field(gt=0)
    parts_per_cycle: int = Field(default=1, gt=0)
    # a new norm applies from now on unless stated otherwise: the past is not rewritten
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StateRulesIn(_Node):
    name: str
    rules: dict[str, Any]
    version: int = 1


@dataclass(frozen=True, slots=True)
class Resource:
    table: str
    model: type[BaseModel]
    kind: str  # reference | site_child | machine | site | organization
    edit: str = REFERENCE_EDIT
    order_by: str = "id"
    json_columns: tuple[str, ...] = ()
    can_create: bool = True
    can_delete: bool = True


_REASON_ORDER = "COALESCE(parent_id, id), parent_id NULLS FIRST, sort_order"
RESOURCES: dict[str, Resource] = {
    "organizations": Resource(
        "organizations", OrganizationIn, "organization", STRUCTURE_EDIT, "name", can_delete=False
    ),
    "sites": Resource("sites", SiteIn, "site", STRUCTURE_EDIT, "name", can_delete=False),
    "areas": Resource("areas", AreaIn, "site_child", STRUCTURE_EDIT, "name"),
    "collectors": Resource("collectors", CollectorIn, "site_child", STRUCTURE_EDIT),
    "machines": Resource(
        "machines", MachineIn, "machine", STRUCTURE_EDIT, "sort_order, code", can_create=False, can_delete=False
    ),
    "shift-templates": Resource(
        "shift_templates", ShiftTemplateIn, "reference", order_by="start_time", json_columns=("planned_breaks",)
    ),
    "downtime-reasons": Resource("downtime_reasons", DowntimeReasonIn, "reference", order_by=_REASON_ORDER),
    "scrap-reasons": Resource("scrap_reasons", ScrapReasonIn, "reference"),
    "programs": Resource("programs", ProgramIn, "reference", NORMS_EDIT, "program, machine_id NULLS FIRST, valid_from"),
    "state-rules": Resource("state_rules", StateRulesIn, "reference", json_columns=("rules",)),
}


def _node(scope: Scope, resource: Resource, row: dict[str, Any]) -> tuple[int, int | None] | None:
    """(org_id, site_id) the row belongs to; None if its site is unknown."""
    if resource.kind == "organization":
        return row["id"], None
    if resource.kind == "site":
        return row["org_id"], row.get("id")
    if resource.kind == "reference":
        return row["org_id"], row.get("site_id")
    org_id = scope.sites.get(row["site_id"])
    return None if org_id is None else (org_id, row["site_id"])


def can_edit(scope: Scope, resource: Resource, row: dict[str, Any]) -> bool:
    if resource.kind == "organization":
        return scope.superadmin
    node = _node(scope, resource, row)
    return node is not None and scope.can_node(resource.edit, *node)


def can_read(scope: Scope, resource: Resource, row: dict[str, Any]) -> bool:
    if can_edit(scope, resource, row):
        return True
    node = _node(scope, resource, row)
    if node is None:
        return False
    if resource.kind == "organization":
        return row["id"] in scope.visible_org_ids
    if resource.kind == "site":
        return row["id"] in scope.visible_site_ids
    if resource.kind == "reference":
        return scope.can_read_node(REFERENCE_VIEW, *node)
    return False  # structure tables are shown in the admin section to those who manage them


def _params(resource: Resource, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    values = dict(values)
    placeholders = {}
    for column in values:
        if column in resource.json_columns:
            values[column] = json.dumps(values[column], ensure_ascii=False)
            placeholders[column] = f"CAST(:{column} AS jsonb)"
        else:
            placeholders[column] = f":{column}"
    return values, placeholders


async def _validate_references(
    session: AsyncSession, scope: Scope, name: str, values: dict[str, Any], row: dict[str, Any]
) -> None:
    """Rows a row points to must live in the same part of the hierarchy."""
    merged = {**row, **values}
    if (
        name == "machines"
        and merged.get("area_id") is not None
        and scope.areas.get(merged["area_id"]) != merged["site_id"]
    ):
        raise InvalidReferenceError("area belongs to another site")
    if name in ("sites",) and merged["org_id"] not in scope.visible_org_ids and not scope.superadmin:
        raise NotFoundError
    if RESOURCES[name].kind == "reference":
        site_id = merged.get("site_id")
        if site_id is not None and scope.sites.get(site_id) != merged["org_id"]:
            raise InvalidReferenceError("site belongs to another organization")
    if name == "downtime-reasons" and merged.get("parent_id") is not None:
        parent = await session.execute(
            text("SELECT 1 FROM downtime_reasons WHERE id = :id AND org_id = :org AND parent_id IS NULL"),
            {"id": merged["parent_id"], "org": merged["org_id"]},
        )
        if parent.first() is None:
            raise InvalidReferenceError("parent reason belongs to another organization")
    if name == "programs" and merged.get("machine_id") is not None:
        machine = next((m for m in scope.machines if m.id == merged["machine_id"]), None)
        site_id = merged.get("site_id")
        if (
            machine is None
            or machine.org_id != merged["org_id"]
            or (site_id is not None and machine.site_id != site_id)
        ):
            raise InvalidReferenceError("machine belongs to another organization or site")


async def list_rows(
    session: AsyncSession, scope: Scope, name: str, org_id: int | None = None, site_id: int | None = None
) -> list[dict[str, Any]]:
    """Rows of a resource the caller may read; optionally of one node only."""
    resource = RESOURCES[name]
    orgs, sites = sorted(scope.visible_org_ids), sorted(scope.visible_site_ids)
    where = {
        "organization": "id = ANY(:orgs)",
        "site": "org_id = ANY(:orgs)",
        "reference": "org_id = ANY(:orgs)",
        "site_child": "site_id = ANY(:sites)",
        "machine": "site_id = ANY(:sites)",
    }[resource.kind]
    rows = await session.execute(
        text(f"SELECT * FROM {resource.table} WHERE {where} ORDER BY {resource.order_by}"),
        {"orgs": orgs, "sites": sites},
    )
    result = []
    for row in (dict(r._mapping) for r in rows):
        if not can_read(scope, resource, row):
            continue
        node = _node(scope, resource, row)
        if org_id is not None and (node is None or node[0] != org_id):
            continue
        if site_id is not None and (node is None or node[1] != site_id):
            continue
        if org_id is not None and site_id is None and resource.kind == "reference" and row.get("site_id") is not None:
            continue  # organization node: defaults only
        result.append({**row, "_editable": can_edit(scope, resource, row)})
    return result


async def get_row(session: AsyncSession, scope: Scope, name: str, row_id: str) -> dict[str, Any]:
    """One row, or NotFoundError if it does not exist for this caller."""
    resource = RESOURCES[name]
    row = (
        await session.execute(text(f"SELECT * FROM {resource.table} WHERE id::text = :row_id"), {"row_id": row_id})
    ).first()
    if row is None or not can_read(scope, resource, dict(row._mapping)):
        raise NotFoundError
    return dict(row._mapping)


async def create_row(session: AsyncSession, scope: Scope, name: str, body: BaseModel) -> dict[str, Any]:
    resource = RESOURCES[name]
    values = body.model_dump()
    if resource.kind == "organization":
        if not scope.superadmin:
            raise ForbiddenError
    else:
        probe = {**values, "id": None} if resource.kind == "site" else values
        node = _node(scope, resource, probe)
        if node is None or (node[0] not in scope.visible_org_ids and not scope.superadmin):
            raise NotFoundError
        if not scope.can_node(resource.edit, node[0], None if resource.kind == "site" else node[1]):
            raise ForbiddenError
    await _validate_references(session, scope, name, values, {})
    params, placeholders = _params(resource, values)
    columns, marks = ", ".join(placeholders), ", ".join(placeholders.values())
    sql = f"INSERT INTO {resource.table} ({columns}) VALUES ({marks}) RETURNING *"
    return dict((await session.execute(text(sql), params)).one()._mapping)


async def update_row(
    session: AsyncSession, scope: Scope, name: str, row_id: str, body: BaseModel
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Returns (row before, row after). The node of a row never changes."""
    resource = RESOURCES[name]
    before = await get_row(session, scope, name, row_id)
    if not can_edit(scope, resource, before):
        raise ForbiddenError
    values = {k: v for k, v in body.model_dump().items() if k not in ("id", "org_id", "site_id")}
    await _validate_references(session, scope, name, values, before)
    params, placeholders = _params(resource, values)
    assignments = ", ".join(f"{column} = {mark}" for column, mark in placeholders.items())
    row = (
        await session.execute(
            text(f"UPDATE {resource.table} SET {assignments} WHERE id::text = :row_id RETURNING *"),
            {**params, "row_id": row_id},
        )
    ).one()
    return before, dict(row._mapping)


async def delete_row(session: AsyncSession, scope: Scope, name: str, row_id: str) -> dict[str, Any]:
    resource = RESOURCES[name]
    before = await get_row(session, scope, name, row_id)
    if not can_edit(scope, resource, before):
        raise ForbiddenError
    await session.execute(text(f"DELETE FROM {resource.table} WHERE id::text = :row_id"), {"row_id": row_id})
    return before
