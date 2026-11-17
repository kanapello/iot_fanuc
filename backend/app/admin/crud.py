"""Generic CRUD over reference tables (column whitelist comes from a pydantic model)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AreaIn(BaseModel):
    site_id: str = "plant1"
    name: str


class MachineIn(BaseModel):
    name: str
    area_id: int | None = None
    controller_type: str | None = None
    is_active: bool = True
    sort_order: int = 0


class ShiftBreak(BaseModel):
    name: str = ""
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class ShiftTemplateIn(BaseModel):
    site_id: str = "plant1"
    name: str
    start_time: time
    end_time: time
    days_of_week: list[int] = [1, 2, 3, 4, 5, 6, 7]
    planned_breaks: list[ShiftBreak] = []
    is_active: bool = True


class DowntimeReasonIn(BaseModel):
    parent_id: int | None = None
    code: str
    name: str
    category: str = Field(default="unplanned", pattern="^(planned|unplanned)$")
    color: str = "#64748b"
    requires_comment: bool = False
    sort_order: int = 0
    is_active: bool = True


class ScrapReasonIn(BaseModel):
    code: str
    name: str
    is_active: bool = True


class ProgramIn(BaseModel):
    machine_id: str | None = None
    program: str
    ideal_cycle_time_s: float = Field(gt=0)
    parts_per_cycle: int = Field(default=1, gt=0)
    # a new norm applies from now on unless stated otherwise: the past is not rewritten
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StateRulesIn(BaseModel):
    name: str
    rules: dict[str, Any]
    version: int = 1


@dataclass(frozen=True, slots=True)
class Resource:
    table: str
    model: type[BaseModel]
    order_by: str = "id"
    json_columns: tuple[str, ...] = ()
    can_create: bool = True
    can_delete: bool = True


RESOURCES: dict[str, Resource] = {
    "areas": Resource("areas", AreaIn),
    "machines": Resource("machines", MachineIn, "sort_order, id", can_create=False, can_delete=False),
    "shift-templates": Resource("shift_templates", ShiftTemplateIn, "start_time", ("planned_breaks",)),
    "downtime-reasons": Resource(
        "downtime_reasons", DowntimeReasonIn, "COALESCE(parent_id, id), parent_id NULLS FIRST, sort_order"
    ),
    "scrap-reasons": Resource("scrap_reasons", ScrapReasonIn),
    "programs": Resource("programs", ProgramIn, "program, machine_id NULLS FIRST"),
    "state-rules": Resource("state_rules", StateRulesIn, json_columns=("rules",), can_delete=False),
}


def _params(resource: Resource, body: BaseModel) -> tuple[dict[str, Any], dict[str, str]]:
    values = body.model_dump()
    placeholders = {}
    for column in values:
        if column in resource.json_columns:
            values[column] = json.dumps(values[column], ensure_ascii=False)
            placeholders[column] = f"CAST(:{column} AS jsonb)"
        else:
            placeholders[column] = f":{column}"
    return values, placeholders


async def list_rows(session: AsyncSession, resource: Resource) -> list[dict[str, Any]]:
    rows = await session.execute(text(f"SELECT * FROM {resource.table} ORDER BY {resource.order_by}"))
    return [dict(r._mapping) for r in rows]


async def get_row(session: AsyncSession, resource: Resource, row_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(text(f"SELECT * FROM {resource.table} WHERE id::text = :row_id"), {"row_id": row_id})
    ).first()
    return dict(row._mapping) if row else None


async def create_row(session: AsyncSession, resource: Resource, body: BaseModel) -> dict[str, Any]:
    values, placeholders = _params(resource, body)
    columns, params = ", ".join(placeholders), ", ".join(placeholders.values())
    sql = f"INSERT INTO {resource.table} ({columns}) VALUES ({params}) RETURNING *"
    row = (await session.execute(text(sql), values)).one()
    return dict(row._mapping)


async def update_row(session: AsyncSession, resource: Resource, row_id: str, body: BaseModel) -> dict[str, Any] | None:
    values, placeholders = _params(resource, body)
    assignments = ", ".join(f"{column} = {placeholder}" for column, placeholder in placeholders.items())
    row = (
        await session.execute(
            text(f"UPDATE {resource.table} SET {assignments} WHERE id::text = :row_id RETURNING *"),
            {**values, "row_id": row_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def delete_row(session: AsyncSession, resource: Resource, row_id: str) -> bool:
    result = await session.execute(text(f"DELETE FROM {resource.table} WHERE id::text = :row_id"), {"row_id": row_id})
    return bool(result.rowcount)  # type: ignore[attr-defined]
