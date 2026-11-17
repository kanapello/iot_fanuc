"""REST API v1. Every route works inside the caller's access scope (app.api.deps.get_scope)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

from app.admin import crud, identity, journal
from app.admin import service as admin
from app.admin.recalc import after_reference_change
from app.api.deps import (
    FORBIDDEN,
    NOT_FOUND,
    AnalyticsMachines,
    CurrentScope,
    Session,
    SettingsDep,
    ViewMachines,
    as_utc,
    get_redis,
    require,
    require_machine,
)
from app.core.access import (
    ANALYTICS_VIEW,
    COLLECTORS_VIEW,
    GEOMETRY_VIEW,
    JOURNAL_EDIT,
    NORMS_EDIT,
    OPERATOR_INPUT,
    REFERENCE_VIEW,
    STRUCTURE_EDIT,
    USER_ROLES,
    USERS_MANAGE,
    Scope,
)
from app.ingest.health import find_seq_gaps
from app.oee import service as oee
from app.reports import service as reports
from app.timeline import service as timeline
from app.worker.live import read_live

router = APIRouter(prefix="/api/v1")
RedisDep = Annotated[Redis | None, Depends(get_redis)]
From = Annotated[datetime, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]


def _range(start: datetime, end: datetime | None) -> tuple[datetime, datetime, datetime]:
    now = datetime.now(UTC)
    start, end = as_utc(start), as_utc(end) if end else now
    if end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' must be after 'from'")
    return start, end, now


def _ids(machines: list[dict[str, Any]]) -> list[int]:
    return [m["id"] for m in machines]


# ---------------------------------------------------------------- auth


class LoginIn(BaseModel):
    username: str
    password: str


@router.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/auth/login", tags=["auth"])
async def auth_login(body: LoginIn, session: Session, settings: SettingsDep) -> dict[str, str]:
    token = await identity.login(session, settings, body.username, body.password)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    return {"token": token}


@router.get("/auth/me", tags=["auth"])
async def auth_me(scope: CurrentScope, session: Session) -> dict[str, Any]:
    """Who the caller is, what sections they may open and which part of the hierarchy they see."""
    tree = await admin.hierarchy(session)
    areas = scope.visible_area_ids()
    kiosk = next((g for g in scope.grants if g.role in ("kiosk", "display")), None)
    return {
        "name": scope.name,
        "kind": scope.kind,
        "superadmin": scope.superadmin,
        "roles": sorted({g.role for g in scope.grants}),
        "menu": {
            "dashboard": bool(scope.machine_ids()),
            "analytics": scope.has(ANALYTICS_VIEW),
            "references": scope.has(REFERENCE_VIEW) or scope.has(NORMS_EDIT),
            "structure": scope.has(STRUCTURE_EDIT),
            "users": scope.has(USERS_MANAGE),
            "service": scope.has(GEOMETRY_VIEW),
        },
        "kiosk": None if kiosk is None else {"machine_id": kiosk.machine_id, "area_id": kiosk.area_id},
        "organizations": [o for o in tree["organizations"] if o["id"] in scope.visible_org_ids],
        "sites": [
            {**s, "areas": [a for a in tree["areas"] if a["site_id"] == s["id"] and a["id"] in areas]}
            for s in tree["sites"]
            if s["id"] in scope.visible_site_ids
        ],
    }


# ---------------------------------------------------------------- machines


async def _with_live(redis: Redis | None, machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    live = await read_live(redis, _ids(machines)) if redis is not None else {}
    return [{**m, "live": live.get(m["id"])} for m in machines]


@router.get("/machines", tags=["machines"])
async def list_machines(machines: ViewMachines, redis: RedisDep) -> list[dict[str, Any]]:
    return await _with_live(redis, machines)


@router.get("/machines/{machine_id}", tags=["machines"])
async def get_machine(machine_id: int, scope: CurrentScope, session: Session, redis: RedisDep) -> dict[str, Any]:
    require_machine(scope, machine_id)
    machines = await admin.list_machines(session, [machine_id], active_only=False)
    if not machines:
        raise NOT_FOUND
    return (await _with_live(redis, machines))[0]


class SetupIn(BaseModel):
    active: bool
    operator: str | None = None


@router.post("/machines/{machine_id}/setup", tags=["operator"])
async def set_setup(machine_id: int, body: SetupIn, scope: CurrentScope, session: Session) -> dict[str, bool]:
    require_machine(scope, machine_id, OPERATOR_INPUT)
    await journal.set_setup(session, machine_id, body.active, body.operator, datetime.now(UTC))
    return {"active": body.active}


async def _site_of(scope: Scope, session: Session, machine_id: int | None, site_id: int | None) -> tuple[int, int]:
    """(org_id, site_id) named by ?machine_id= or ?site_id=, if the caller can see it."""
    if machine_id is not None:
        require_machine(scope, machine_id)
        node = next(m for m in scope.machines if m.id == machine_id)
        return node.org_id, node.site_id
    if site_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "machine_id or site_id is required")
    if site_id not in scope.visible_site_ids:
        raise NOT_FOUND
    return scope.sites[site_id], site_id


@router.get("/reasons", tags=["reference"])
async def list_reasons(
    scope: CurrentScope, session: Session, machine_id: int | None = None, site_id: int | None = None
) -> list[dict[str, Any]]:
    """Downtime reasons in effect on a site, as a two-level tree."""
    rows = await admin.downtime_reasons(session, *await _site_of(scope, session, machine_id, site_id))
    groups = [{**r, "children": []} for r in rows if r["parent_id"] is None]
    by_id = {g["id"]: g for g in groups}
    for r in rows:
        if r["parent_id"] in by_id:
            by_id[r["parent_id"]]["children"].append(r)
    return groups


@router.get("/scrap-reasons", tags=["reference"])
async def list_scrap_reasons(
    scope: CurrentScope, session: Session, machine_id: int | None = None, site_id: int | None = None
) -> list[dict[str, Any]]:
    return await admin.scrap_reasons(session, *await _site_of(scope, session, machine_id, site_id))


@router.get("/shifts", tags=["reference"])
async def list_shifts(
    scope: CurrentScope, session: Session, site_id: int, start: From, end: To = None
) -> list[dict[str, Any]]:
    """Shift instances of a site overlapping the period."""
    start, end, _now = _range(start, end)
    org_id, site_id = await _site_of(scope, session, None, site_id)
    site = next(s for s in (await admin.hierarchy(session))["sites"] if s["id"] == site_id)
    node = {"org_id": org_id, "site_id": site_id, "timezone": site["timezone"]}
    return [
        {"name": s.name, "start": s.start, "end": s.end, "breaks": [{"start": b0, "end": b1} for b0, b1 in s.breaks]}
        for s in await oee.site_shifts(session, node, start, end)
    ]


@router.get("/collectors", tags=["system"])
async def list_collectors(scope: CurrentScope, session: Session, settings: SettingsDep) -> list[dict[str, Any]]:
    """Collector state: heartbeat, online flag, lost buffer and gaps in message sequence numbers."""
    require(scope, COLLECTORS_VIEW)
    now = datetime.now(UTC)
    sites = [s for s, org in scope.sites.items() if scope.can_node(COLLECTORS_VIEW, org, s)]
    window = timedelta(hours=settings.seq_check_window_hours)
    result = []
    for collector in await admin.list_collectors(session, sites):
        gaps = await find_seq_gaps(session, collector["machine_ids"], now, settings.seq_check_window_hours)
        heartbeat, lost = collector["last_heartbeat"], collector["buffer_lost_at"]
        result.append(
            {
                **collector,
                "online": heartbeat is not None
                and (now - heartbeat).total_seconds() <= settings.collector_offline_after_s,
                # the collector started with an empty buffer: unsent data is gone, seq numbering began again
                "buffer_lost": lost is not None and now - lost <= window,
                "seq_gaps": gaps,
                "missing_messages": sum(g["missing"] for g in gaps),
            }
        )
    return result


# ---------------------------------------------------------------- timeline / OEE / reports


@router.get("/timeline", tags=["analytics"])
async def get_timeline(session: Session, machines: AnalyticsMachines, start: From, end: To = None) -> dict[str, Any]:
    start, end, now = _range(start, end)
    intervals = await timeline.get_intervals(session, _ids(machines), start, end)
    for interval in intervals:
        interval["start_ts"] = max(interval["start_ts"], start)
        interval["end_ts"] = min(interval["end_ts"] or now, end)
    return {
        "from": start,
        "to": end,
        "intervals": intervals,
        "annotations": await journal.list_annotations(session, _ids(machines), start, end),
    }


@router.get("/oee", tags=["analytics"])
async def get_oee(
    session: Session,
    machines: AnalyticsMachines,
    start: From,
    end: To = None,
    period: Annotated[str, Query(pattern="^(hour|shift|day|total)$")] = "shift",
    group: Annotated[str, Query(pattern="^(machine|area)$")] = "machine",
) -> list[dict[str, Any]]:
    start, end, _now = _range(start, end)
    return await oee.query(session, machines, period, start, end, group_machines=group == "area")


@router.get("/reports/pareto", tags=["analytics"])
async def get_pareto(
    session: Session, machines: AnalyticsMachines, start: From, end: To = None
) -> list[dict[str, Any]]:
    start, end, now = _range(start, end)
    return await reports.downtime_pareto(session, _ids(machines), start, end, now)


@router.get("/annotations", tags=["analytics"])
async def list_annotations(
    session: Session, machines: AnalyticsMachines, start: From, end: To = None
) -> list[dict[str, Any]]:
    start, end, _now = _range(start, end)
    return await journal.list_annotations(session, _ids(machines), start, end)


@router.get("/scrap", tags=["analytics"])
async def list_scrap(
    session: Session, machines: AnalyticsMachines, start: From, end: To = None
) -> list[dict[str, Any]]:
    start, end, _now = _range(start, end)
    return await journal.list_scrap(session, _ids(machines), start, end)


# ---------------------------------------------------------------- operator input


class AnnotationIn(BaseModel):
    machine_id: int
    start_ts: datetime
    end_ts: datetime | None = None
    reason_id: int
    comment: str | None = None
    operator: str | None = None


@router.post("/annotations", tags=["operator"], status_code=status.HTTP_201_CREATED)
async def create_annotation(body: AnnotationIn, scope: CurrentScope, session: Session) -> dict[str, int]:
    require_machine(scope, body.machine_id, OPERATOR_INPUT)
    end = as_utc(body.end_ts) if body.end_ts else None
    try:
        new_id = await journal.add_annotation(
            session, body.machine_id, as_utc(body.start_ts), end, body.reason_id, body.comment, body.operator
        )
    except journal.ForeignReasonError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown reason") from exc
    return {"id": new_id}


@router.delete("/annotations/{annotation_id}", tags=["operator"], status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(annotation_id: int, scope: CurrentScope, session: Session) -> None:
    machine_id = await journal.annotation_machine(session, annotation_id)
    if machine_id is None:
        raise NOT_FOUND
    require_machine(scope, machine_id, JOURNAL_EDIT)
    await journal.delete_annotation(session, annotation_id, scope.machine_ids(JOURNAL_EDIT))


class ScrapIn(BaseModel):
    machine_id: int
    qty: int = Field(gt=0, le=10000)
    reason_id: int | None = None
    program: str | None = None
    operator: str | None = None
    ts: datetime | None = None


@router.post("/scrap", tags=["operator"], status_code=status.HTTP_201_CREATED)
async def create_scrap(body: ScrapIn, scope: CurrentScope, session: Session) -> dict[str, int]:
    require_machine(scope, body.machine_id, OPERATOR_INPUT)
    ts = as_utc(body.ts) if body.ts else datetime.now(UTC)
    try:
        new_id = await journal.add_scrap(
            session, body.machine_id, ts, body.qty, body.reason_id, body.program, body.operator
        )
    except journal.ForeignReasonError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown scrap reason") from exc
    return {"id": new_id}


# ---------------------------------------------------------------- references and structure (CRUD)


def _resource(name: str) -> crud.Resource:
    resource = crud.RESOURCES.get(name)
    if resource is None:
        raise NOT_FOUND
    return resource


def _parse(resource: crud.Resource, body: dict[str, Any]) -> BaseModel:
    try:
        return resource.model.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


async def _guarded(action: Any) -> Any:
    """Translate scope and integrity errors of CRUD into HTTP answers."""
    try:
        return await action
    except crud.NotFoundError as exc:
        raise NOT_FOUND from exc
    except crud.ForbiddenError as exc:
        raise FORBIDDEN from exc
    except crud.InvalidReferenceError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "conflicts with existing data") from exc


@router.get("/admin/{name}", tags=["admin"])
async def admin_list(
    name: str, scope: CurrentScope, session: Session, org_id: int | None = None, site_id: int | None = None
) -> list[dict[str, Any]]:
    _resource(name)
    return await crud.list_rows(session, scope, name, org_id, site_id)


@router.post("/admin/{name}", tags=["admin"], status_code=status.HTTP_201_CREATED)
async def admin_create(
    name: str, body: dict[str, Any], scope: CurrentScope, session: Session, settings: SettingsDep
) -> dict[str, Any]:
    resource = _resource(name)
    if not resource.can_create:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "rows of this resource are created automatically")
    row: dict[str, Any] = await _guarded(crud.create_row(session, scope, name, _parse(resource, body)))
    await after_reference_change(session, name, None, row, datetime.now(UTC), settings.recalc_horizon_days)
    return row


@router.put("/admin/{name}/{row_id}", tags=["admin"])
async def admin_update(
    name: str, row_id: str, body: dict[str, Any], scope: CurrentScope, session: Session, settings: SettingsDep
) -> dict[str, Any]:
    resource = _resource(name)
    current: dict[str, Any] = await _guarded(crud.get_row(session, scope, name, row_id))
    # the node of a row never changes; editing a norm keeps its start of validity unless stated
    keep = ("org_id", "site_id", "id", "code") if resource.kind != "reference" else ("org_id", "site_id")
    merged = {**body, **{k: current[k] for k in keep if k in current}}
    if name == "programs" and "valid_from" not in body:
        merged["valid_from"] = current["valid_from"]
    before, row = await _guarded(crud.update_row(session, scope, name, row_id, _parse(resource, merged)))
    await after_reference_change(session, name, before, row, datetime.now(UTC), settings.recalc_horizon_days)
    return dict(row)


@router.delete("/admin/{name}/{row_id}", tags=["admin"], status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete(name: str, row_id: str, scope: CurrentScope, session: Session, settings: SettingsDep) -> None:
    resource = _resource(name)
    if not resource.can_delete:
        raise HTTPException(status.HTTP_405_METHOD_NOT_ALLOWED, "rows of this resource cannot be deleted")
    before = await _guarded(crud.delete_row(session, scope, name, row_id))
    await after_reference_change(session, name, before, None, datetime.now(UTC), settings.recalc_horizon_days)


@router.post("/recalc", tags=["admin"])
async def recalc(
    scope: CurrentScope,
    session: Session,
    hours: Annotated[int, Query(ge=1, le=24 * 31)] = 24,
    site_id: int | None = None,
) -> dict[str, int]:
    """Manually ask the worker to rebuild timeline and OEE of the last N hours."""
    require(scope, STRUCTURE_EDIT)
    machines = [
        m.id
        for m in scope.machines
        if scope.can_node(STRUCTURE_EDIT, m.org_id, m.site_id) and site_id in (None, m.site_id)
    ]
    await timeline.mark_dirty(session, {m: datetime.now(UTC) - timedelta(hours=hours) for m in machines})
    return {"machines": len(machines)}


# ---------------------------------------------------------------- users, roles, kiosk tokens


class MembershipIn(BaseModel):
    role: str
    org_id: int | None = None
    site_id: int | None = None
    area_id: int | None = None


class UserIn(BaseModel):
    login: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=4)
    full_name: str = ""
    membership: MembershipIn


class UserUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = Field(default=None, min_length=4)
    is_active: bool | None = None


def _managed_orgs(scope: Scope) -> list[int] | None:
    """Organizations whose users the caller manages; None = all (superadmin)."""
    if scope.superadmin:
        return None
    return sorted({g.org_id for g in scope.grants if g.allows(USERS_MANAGE) and g.org_id is not None})


def _check_membership(scope: Scope, membership: MembershipIn) -> None:
    """A role may be granted only on a node the caller administers."""
    if membership.role not in USER_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown role")
    if membership.role == "service" or membership.org_id is None:
        # service engineers see every customer: only the superadmin appoints them
        if not scope.superadmin or membership.role != "service" or membership.site_id or membership.area_id:
            raise FORBIDDEN
        return
    org_id, site_id, area_id = membership.org_id, membership.site_id, membership.area_id
    if org_id not in scope.visible_org_ids and not scope.superadmin:
        raise NOT_FOUND
    if site_id is not None and scope.sites.get(site_id) != org_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "site belongs to another organization")
    if area_id is not None and (site_id is None or scope.areas.get(area_id) != site_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "area belongs to another site")
    if not scope.can_node(USERS_MANAGE, org_id, site_id, area_id):
        raise FORBIDDEN


async def _managed_user(scope: Scope, session: Session, user_id: int) -> dict[str, Any]:
    user = next((u for u in await identity.list_users(session, _managed_orgs(scope)) if u["id"] == user_id), None)
    if user is None:
        raise NOT_FOUND
    if user["is_superadmin"] and not scope.superadmin:
        raise FORBIDDEN
    return user


@router.get("/users", tags=["users"])
async def list_users(scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    require(scope, USERS_MANAGE)
    return await identity.list_users(session, _managed_orgs(scope))


@router.post("/users", tags=["users"], status_code=status.HTTP_201_CREATED)
async def create_user(body: UserIn, scope: CurrentScope, session: Session) -> dict[str, int]:
    require(scope, USERS_MANAGE)
    _check_membership(scope, body.membership)
    try:
        async with session.begin_nested():
            user_id = await identity.create_user(session, body.login, body.password, body.full_name)
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "login is already taken") from exc
    m = body.membership
    await identity.add_membership(session, user_id, m.role, m.org_id, m.site_id, m.area_id)
    return {"id": user_id}


@router.put("/users/{user_id}", tags=["users"])
async def update_user(user_id: int, body: UserUpdate, scope: CurrentScope, session: Session) -> dict[str, int]:
    require(scope, USERS_MANAGE)
    await _managed_user(scope, session, user_id)
    await identity.update_user(session, user_id, body.full_name, body.password, body.is_active)
    return {"id": user_id}


@router.post("/users/{user_id}/memberships", tags=["users"], status_code=status.HTTP_201_CREATED)
async def add_membership(user_id: int, body: MembershipIn, scope: CurrentScope, session: Session) -> dict[str, int]:
    require(scope, USERS_MANAGE)
    await _managed_user(scope, session, user_id)
    _check_membership(scope, body)
    return {"id": await identity.add_membership(session, user_id, body.role, body.org_id, body.site_id, body.area_id)}


@router.delete("/memberships/{membership_id}", tags=["users"], status_code=status.HTTP_204_NO_CONTENT)
async def delete_membership(membership_id: int, scope: CurrentScope, session: Session) -> None:
    require(scope, USERS_MANAGE)
    row = await identity.get_membership(session, membership_id)
    if row is None or (row["org_id"] is None and not scope.superadmin):
        raise NOT_FOUND
    if row["org_id"] is not None:
        if row["org_id"] not in scope.visible_org_ids and not scope.superadmin:
            raise NOT_FOUND
        if not scope.can_node(USERS_MANAGE, row["org_id"], row["site_id"], row["area_id"]):
            raise FORBIDDEN
    await identity.delete_membership(session, membership_id)


class DeviceTokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    machine_id: int | None = None
    area_id: int | None = None


def _token_target(scope: Scope, machine_id: int | None, area_id: int | None) -> None:
    """A kiosk token is bound to exactly one machine or one area the caller administers."""
    if (machine_id is None) == (area_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "exactly one of machine_id, area_id is required")
    if machine_id is not None:
        node = next((m for m in scope.machines if m.id == machine_id), None)
        if node is None or not scope.can_machine("machine.view", machine_id):
            raise NOT_FOUND
        org_id, site_id = node.org_id, node.site_id
    else:
        site = scope.areas.get(area_id or -1)
        if site is None or area_id not in scope.visible_area_ids():
            raise NOT_FOUND
        org_id, site_id = scope.sites[site], site
    if not scope.can_node(STRUCTURE_EDIT, org_id, site_id):
        raise FORBIDDEN


@router.get("/device-tokens", tags=["users"])
async def list_device_tokens(scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    require(scope, STRUCTURE_EDIT)
    machines = [m.id for m in scope.machines if scope.can_node(STRUCTURE_EDIT, m.org_id, m.site_id)]
    areas = [a for a, site in scope.areas.items() if scope.can_node(STRUCTURE_EDIT, scope.sites[site], site)]
    return await identity.list_device_tokens(session, machines, areas)


@router.post("/device-tokens", tags=["users"], status_code=status.HTTP_201_CREATED)
async def issue_device_token(body: DeviceTokenIn, scope: CurrentScope, session: Session) -> dict[str, Any]:
    """Issue a kiosk token. The value is returned once and is not stored in readable form."""
    require(scope, STRUCTURE_EDIT)
    _token_target(scope, body.machine_id, body.area_id)
    token_id, token = await identity.issue_device_token(
        session, body.name, body.machine_id, body.area_id, scope.user_id
    )
    return {"id": token_id, "token": token}


@router.delete("/device-tokens/{token_id}", tags=["users"], status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device_token(token_id: int, scope: CurrentScope, session: Session) -> None:
    require(scope, STRUCTURE_EDIT)
    row = await identity.get_device_token(session, token_id)
    if row is None:
        raise NOT_FOUND
    _token_target(scope, row["machine_id"], row["area_id"])
    await identity.revoke_device_token(session, token_id)
