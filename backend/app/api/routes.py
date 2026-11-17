"""REST API v1: machines, timeline, OEE, operator input, reports, reference data."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

from app.admin import crud, journal
from app.admin import service as admin
from app.admin.recalc import after_reference_change
from app.api.deps import Admin, Machines, Session, User, as_utc, get_redis, get_settings_dep
from app.core.config import Settings
from app.core.security import login
from app.ingest.health import find_seq_gaps
from app.oee import service as oee
from app.reports import service as reports
from app.timeline import service as timeline
from app.worker.live import read_live

router = APIRouter(prefix="/api/v1")
RedisDep = Annotated[Redis, Depends(get_redis)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
From = Annotated[datetime, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]


def _range(start: datetime, end: datetime | None) -> tuple[datetime, datetime, datetime]:
    now = datetime.now(UTC)
    start, end = as_utc(start), as_utc(end) if end else now
    if end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' must be after 'from'")
    return start, end, now


class LoginIn(BaseModel):
    username: str
    password: str


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/auth/login", tags=["auth"])
async def auth_login(body: LoginIn, settings: Annotated[Settings, Depends(get_settings_dep)]) -> dict[str, str]:
    token = login(settings, body.username, body.password)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    return {"token": token, "role": "admin"}


@router.get("/auth/me", tags=["auth"])
async def auth_me(user: User) -> dict[str, str]:
    return {"name": user.name, "role": user.role}


# ---------------------------------------------------------------- machines


async def _with_live(redis: Redis, machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live = await read_live(redis, [m["id"] for m in machines])
    return [{**m, "live": live.get(m["id"])} for m in machines]


@router.get("/machines", tags=["machines"])
async def list_machines(_: User, machines: Machines, redis: RedisDep) -> list[dict[str, Any]]:
    return await _with_live(redis, machines)


@router.get("/machines/{machine_id}", tags=["machines"])
async def get_machine(machine_id: str, _: User, session: Session, redis: RedisDep) -> dict[str, Any]:
    machine = await admin.get_machine(session, machine_id)
    if machine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "machine not found")
    return (await _with_live(redis, [machine]))[0]


class SetupIn(BaseModel):
    active: bool
    operator: str | None = None


@router.post("/machines/{machine_id}/setup", tags=["operator"])
async def set_setup(machine_id: str, body: SetupIn, _: User, session: Session) -> dict[str, bool]:
    await journal.set_setup(session, machine_id, body.active, body.operator, datetime.now(UTC))
    return {"active": body.active}


@router.get("/collectors", tags=["system"])
async def list_collectors(_: User, session: Session, settings: SettingsDep) -> list[dict[str, Any]]:
    """Collector state: heartbeat, online flag and gaps in message sequence numbers (lost data)."""
    now = datetime.now(UTC)
    gaps = await find_seq_gaps(session, now, settings.seq_check_window_hours)
    result = []
    for collector in await admin.list_collectors(session):
        heartbeat = collector["last_heartbeat"]
        online = heartbeat is not None and (now - heartbeat).total_seconds() <= settings.collector_offline_after_s
        own = [g for g in gaps if g["collector_id"] == collector["id"]]
        result.append(
            {**collector, "online": online, "seq_gaps": own, "missing_messages": sum(g["missing"] for g in own)}
        )
    return result


@router.get("/areas", tags=["reference"])
async def list_areas(_: User, session: Session) -> list[dict[str, Any]]:
    return await crud.list_rows(session, crud.RESOURCES["areas"])


@router.get("/reasons", tags=["reference"])
async def list_reasons(_: User, session: Session) -> list[dict[str, Any]]:
    """Active downtime reasons as a two-level tree."""
    rows = [r for r in await crud.list_rows(session, crud.RESOURCES["downtime-reasons"]) if r["is_active"]]
    groups = [{**r, "children": []} for r in rows if r["parent_id"] is None]
    by_id = {g["id"]: g for g in groups}
    for r in rows:
        if r["parent_id"] in by_id:
            by_id[r["parent_id"]]["children"].append(r)
    return sorted(groups, key=lambda g: g["sort_order"])


@router.get("/scrap-reasons", tags=["reference"])
async def list_scrap_reasons(_: User, session: Session) -> list[dict[str, Any]]:
    return [r for r in await crud.list_rows(session, crud.RESOURCES["scrap-reasons"]) if r["is_active"]]


@router.get("/shifts", tags=["reference"])
async def list_shifts(
    _: User, session: Session, machines: Machines, start: From, end: To = None
) -> list[dict[str, Any]]:
    """Shift instances overlapping the period (calendar of the machines' site)."""
    start, end, _now = _range(start, end)
    if not machines:
        return []
    return [
        {"name": s.name, "start": s.start, "end": s.end, "breaks": [{"start": b0, "end": b1} for b0, b1 in s.breaks]}
        for s in await oee.site_shifts(session, machines[0], start, end)
    ]


# ---------------------------------------------------------------- timeline / OEE / reports


@router.get("/timeline", tags=["analytics"])
async def get_timeline(_: User, session: Session, machines: Machines, start: From, end: To = None) -> dict[str, Any]:
    start, end, now = _range(start, end)
    ids = [m["id"] for m in machines]
    intervals = await timeline.get_intervals(session, ids, start, end)
    for interval in intervals:
        interval["start_ts"] = max(interval["start_ts"], start)
        interval["end_ts"] = min(interval["end_ts"] or now, end)
    return {
        "from": start,
        "to": end,
        "intervals": intervals,
        "annotations": await journal.list_annotations(session, ids, start, end),
    }


@router.get("/oee", tags=["analytics"])
async def get_oee(
    _: User,
    session: Session,
    machines: Machines,
    start: From,
    end: To = None,
    period: Annotated[str, Query(pattern="^(hour|shift|day|total)$")] = "shift",
    group: Annotated[str, Query(pattern="^(machine|area)$")] = "machine",
) -> list[dict[str, Any]]:
    start, end, _now = _range(start, end)
    return await oee.query(session, machines, period, start, end, group_machines=group == "area")


@router.get("/reports/pareto", tags=["analytics"])
async def get_pareto(
    _: User, session: Session, machines: Machines, start: From, end: To = None
) -> list[dict[str, Any]]:
    start, end, now = _range(start, end)
    return await reports.downtime_pareto(session, [m["id"] for m in machines], start, end, now)


# ---------------------------------------------------------------- operator input


class AnnotationIn(BaseModel):
    machine_id: str
    start_ts: datetime
    end_ts: datetime | None = None
    reason_id: int
    comment: str | None = None
    operator: str | None = None


@router.get("/annotations", tags=["operator"])
async def list_annotations(
    _: User, session: Session, machines: Machines, start: From, end: To = None
) -> list[dict[str, Any]]:
    start, end, _now = _range(start, end)
    return await journal.list_annotations(session, [m["id"] for m in machines], start, end)


@router.post("/annotations", tags=["operator"], status_code=status.HTTP_201_CREATED)
async def create_annotation(body: AnnotationIn, _: User, session: Session) -> dict[str, int]:
    try:
        new_id = await journal.add_annotation(
            session, body.machine_id, as_utc(body.start_ts), as_utc(body.end_ts) if body.end_ts else None,
            body.reason_id, body.comment, body.operator,
        )  # fmt: skip
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown reason") from exc
    return {"id": new_id}


@router.delete("/annotations/{annotation_id}", tags=["operator"], status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(annotation_id: int, _: User, session: Session) -> None:
    if not await journal.delete_annotation(session, annotation_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "annotation not found")


class ScrapIn(BaseModel):
    machine_id: str
    qty: int = Field(gt=0, le=10000)
    reason_id: int | None = None
    program: str | None = None
    operator: str | None = None
    ts: datetime | None = None


@router.get("/scrap", tags=["operator"])
async def list_scrap(
    _: User, session: Session, machines: Machines, start: From, end: To = None
) -> list[dict[str, Any]]:
    start, end, _now = _range(start, end)
    return await journal.list_scrap(session, [m["id"] for m in machines], start, end)


@router.post("/scrap", tags=["operator"], status_code=status.HTTP_201_CREATED)
async def create_scrap(body: ScrapIn, _: User, session: Session) -> dict[str, int]:
    ts = as_utc(body.ts) if body.ts else datetime.now(UTC)
    try:
        new_id = await journal.add_scrap(
            session, body.machine_id, ts, body.qty, body.reason_id, body.program, body.operator
        )
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown scrap reason") from exc
    return {"id": new_id}


# ---------------------------------------------------------------- admin CRUD


def _resource(name: str) -> crud.Resource:
    resource = crud.RESOURCES.get(name)
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown resource")
    return resource


def _parse(resource: crud.Resource, body: dict[str, Any]) -> BaseModel:
    try:
        return resource.model.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, json.loads(json.dumps(str(exc)))) from exc


@router.get("/admin/{name}", tags=["admin"])
async def admin_list(name: str, _: Admin, session: Session) -> list[dict[str, Any]]:
    return await crud.list_rows(session, _resource(name))


async def _recalc(session: Any, settings: Settings, name: str, before: Any, after: Any) -> None:
    await after_reference_change(session, name, before, after, datetime.now(UTC), settings.recalc_horizon_days)


@router.post("/admin/{name}", tags=["admin"], status_code=status.HTTP_201_CREATED)
async def admin_create(
    name: str, body: dict[str, Any], _: Admin, session: Session, settings: SettingsDep
) -> dict[str, Any]:
    resource = _resource(name)
    if not resource.can_create:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "rows of this resource are created automatically")
    try:
        row = await crud.create_row(session, resource, _parse(resource, body))
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "conflicts with existing data") from exc
    await _recalc(session, settings, name, None, row)
    return row


@router.put("/admin/{name}/{row_id}", tags=["admin"])
async def admin_update(
    name: str, row_id: str, body: dict[str, Any], _: Admin, session: Session, settings: SettingsDep
) -> dict[str, Any]:
    resource = _resource(name)
    before = await crud.get_row(session, resource, row_id)
    if before is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "row not found")
    if name == "programs" and "valid_from" not in body:
        body = {**body, "valid_from": before["valid_from"]}  # editing a norm keeps its start of validity
    try:
        row = await crud.update_row(session, resource, row_id, _parse(resource, body))
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "conflicts with existing data") from exc
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "row not found")
    await _recalc(session, settings, name, before, row)
    return row


@router.delete("/admin/{name}/{row_id}", tags=["admin"], status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete(name: str, row_id: str, _: Admin, session: Session, settings: SettingsDep) -> None:
    resource = _resource(name)
    if not resource.can_delete:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "rows of this resource cannot be deleted")
    before = await crud.get_row(session, resource, row_id)
    try:
        deleted = await crud.delete_row(session, resource, row_id)
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "row is referenced by other data") from exc
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "row not found")
    await _recalc(session, settings, name, before, None)


@router.post("/recalc", tags=["admin"])
async def admin_recalc(
    _: Admin, session: Session, machines: Machines, hours: Annotated[int, Query(ge=1, le=24 * 31)] = 24
) -> dict[str, int]:
    """Manually ask the worker to rebuild timeline and OEE of the last N hours."""
    start = datetime.now(UTC) - timedelta(hours=hours)
    await timeline.mark_dirty(session, {m["id"]: start for m in machines})
    return {"machines": len(machines)}
