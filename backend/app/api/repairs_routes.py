"""Service repairs API (ADR 0006): alarm journal, repair requests, maintenance by running hours.

Customers (master, engineer, administrator) see the requests and the maintenance schedule of their
machines and open requests; the service takes requests through to done and keeps the maintenance
plans. The geometry archive stays closed to customers (``geometry_routes``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.admin import identity
from app.admin import service as admin
from app.api.deps import (
    FORBIDDEN,
    NOT_FOUND,
    AnalyticsMachines,
    CurrentScope,
    From,
    RepairsMachines,
    Session,
    To,
    VoidBody,
    as_utc,
    require,
    require_machine,
    time_range,
)
from app.core.access import ANALYTICS_VIEW, REPAIRS_MANAGE, REPAIRS_REQUEST, REPAIRS_VIEW, Scope
from app.repairs import maintenance, requests
from app.reports import alarms

router = APIRouter(prefix="/api/v1")
CLOCK_SKEW = timedelta(minutes=5)  # a moment "now" from a client may be slightly ahead of the server


def _bad_input(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)


def _not_future(value: datetime | None, now: datetime, what: str) -> datetime:
    moment = as_utc(value) if value else now
    if moment > now + CLOCK_SKEW:
        raise _bad_input(f"{what} is in the future")
    return min(moment, now)


async def _who(scope: Scope, session: Session) -> str:
    """The caller as people know them: full name, else login (or the name of a device token)."""
    user = await identity.get_user(session, scope.user_id) if scope.user_id is not None else None
    return str(user["full_name"] or user["login"]) if user else scope.name


async def _with_machines(session: Session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name, organization, site and area of the machine of every row."""
    machines = {m["id"]: m for m in await admin.list_machines(session, {r["machine_id"] for r in rows}, False)}
    for row in rows:
        machine = machines[row["machine_id"]]
        row.update(
            machine_name=machine["name"],
            org_name=machine["org_name"],
            site_name=machine["site_name"],
            area_name=machine["area_name"],
        )
    return rows


# ---------------------------------------------------------------- alarm journal


@router.get("/alarms", tags=["alarms"])
async def alarm_journal(
    scope: CurrentScope,
    session: Session,
    machines: AnalyticsMachines,
    start: From,
    end: To = None,
    code: Annotated[str | None, Query(max_length=32)] = None,
) -> list[dict[str, Any]]:
    """Alarm episodes that started in the period: code, text, start, end, duration; newest first.

    ``request_id`` is the repair request opened from the episode, if there is one.
    """
    start, end, now = time_range(start, end)
    found = await alarms.episodes(session, [m["id"] for m in machines], start, end, now, code)
    linked = await requests.for_alarms(session, [(e["machine_id"], e["start_ts"], e["code"]) for e in found])
    for episode in found:
        key = (episode["machine_id"], episode["start_ts"], episode["code"])
        visible = scope.can_machine(REPAIRS_VIEW, episode["machine_id"])
        episode["request_id"] = linked.get(key) if visible else None
    return await _with_machines(session, found)


@router.get("/alarms/frequency", tags=["alarms"])
async def alarm_frequency(
    session: Session, machines: AnalyticsMachines, start: From, end: To = None
) -> list[dict[str, Any]]:
    """Per machine over the period: number of alarm episodes, per day, total duration, and by alarm code."""
    start, end, now = time_range(start, end)
    ids = [m["id"] for m in machines]
    found = await alarms.episodes(session, ids, start, end, now)
    days = (end - start).total_seconds() / 86400
    return await _with_machines(session, alarms.frequency(found, ids, days))


@router.get("/machines/{machine_id}/alarm-context", tags=["alarms"])
async def alarm_context(machine_id: int, ts: datetime, scope: CurrentScope, session: Session) -> dict[str, Any]:
    """Telemetry of the 10 minutes before an alarm and the machine events of that window."""
    require_machine(scope, machine_id, ANALYTICS_VIEW)
    return await alarms.context(session, machine_id, as_utc(ts))


# ---------------------------------------------------------------- repair requests


class RequestIn(BaseModel):
    machine_id: int
    description: str = Field(default="", max_length=2000)
    # from an alarm of the journal: its start and code; otherwise the moment of the failure (default: now)
    alarm_ts: datetime | None = None
    alarm_code: str | None = Field(default=None, max_length=32)
    failure_ts: datetime | None = None


class StatusIn(BaseModel):
    status: Literal["accepted", "in_progress", "done"]
    executor_id: int | None = None
    work_done: str | None = Field(default=None, max_length=4000)
    comment: str | None = Field(default=None, max_length=1000)
    at: datetime | None = None  # when it happened, if entered later; default now


async def _request(scope: Scope, session: Session, request_id: int, permission: str) -> dict[str, Any]:
    request = await requests.get(session, request_id)
    if request is None:
        raise NOT_FOUND
    require_machine(scope, request["machine_id"], permission)
    return request


@router.post("/repair-requests", tags=["repairs"], status_code=status.HTTP_201_CREATED)
async def open_request(body: RequestIn, scope: CurrentScope, session: Session) -> dict[str, int]:
    """Open a repair request: with one click from an alarm of the journal, or by hand."""
    require_machine(scope, body.machine_id, REPAIRS_REQUEST)
    now = datetime.now(UTC)
    alarm = None
    if body.alarm_ts is not None or body.alarm_code is not None:
        if body.alarm_ts is None or body.alarm_code is None:
            raise _bad_input("alarm_ts and alarm_code go together")
        alarm = await alarms.find_episode(session, body.machine_id, as_utc(body.alarm_ts), body.alarm_code, now)
        if alarm is None:
            raise _bad_input("no such alarm on this machine")
        if await requests.for_alarms(session, [(body.machine_id, alarm["start_ts"], alarm["code"])]):
            raise HTTPException(status.HTTP_409_CONFLICT, "a request for this alarm already exists")
        failure_ts = alarm["start_ts"]
        description = body.description.strip() or f"{alarm['code']} {alarm['message'] or ''}".strip()
    else:
        failure_ts = _not_future(body.failure_ts, now, "the failure")
        description = body.description.strip()
        if len(description) < 3:
            raise _bad_input("describe the failure")
    try:
        async with session.begin_nested():
            request_id = await requests.create(
                session, body.machine_id, description, failure_ts, alarm, scope.user_id, await _who(scope, session)
            )
    except IntegrityError as exc:  # the same alarm, at the same moment, from someone else
        raise HTTPException(status.HTTP_409_CONFLICT, "a request for this alarm already exists") from exc
    return {"id": request_id}


@router.get("/repair-requests", tags=["repairs"])
async def list_requests(
    session: Session, machines: RepairsMachines, open_only: Annotated[bool, Query(alias="open")] = False
) -> list[dict[str, Any]]:
    """Requests of the caller's machines, newest first, with reaction and restoration times."""
    rows = await requests.list_requests(session, [m["id"] for m in machines], open_only)
    return await _with_machines(session, await requests.with_times(session, rows))


@router.get("/repair-requests/{request_id}", tags=["repairs"])
async def get_request(request_id: int, scope: CurrentScope, session: Session) -> dict[str, Any]:
    request = await _request(scope, session, request_id, REPAIRS_VIEW)
    (request,) = await _with_machines(session, await requests.with_times(session, [request]))
    request["history"] = await requests.history(session, request_id)
    return request


@router.post("/repair-requests/{request_id}/status", tags=["repairs"])
async def change_status(request_id: int, body: StatusIn, scope: CurrentScope, session: Session) -> dict[str, Any]:
    """The service moves a request forward: accepted (with the executor) -> in progress -> done.

    Done writes the repair into the log with a link to the request. Posting the current status with
    another ``executor_id`` changes the executor.
    """
    request = await _request(scope, session, request_id, REPAIRS_MANAGE)
    executor = None
    if body.executor_id is not None:
        engineer = next(
            (u for u in await identity.users_with_role(session, "service") if u["id"] == body.executor_id), None
        )
        if engineer is None:
            raise _bad_input("the executor must be an active user with the role 'service'")
        executor = {"id": engineer["id"], "name": engineer["full_name"] or engineer["login"]}
    now = datetime.now(UTC)
    at = _not_future(body.at, now, "the moment")
    try:
        await requests.advance(
            session,
            request,
            body.status,
            at,
            now,
            executor,
            body.work_done,
            body.comment,
            scope.user_id,
            await _who(scope, session),
        )
    except requests.RequestConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except requests.RequestInputError as exc:
        raise _bad_input(str(exc)) from exc
    return {"id": request_id, "status": body.status}


@router.post("/repair-requests/{request_id}/void", tags=["repairs"])
async def void_request(request_id: int, body: VoidBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """A request opened by mistake is voided with a reason.

    The customer may withdraw a request nobody has accepted yet; the service, any request until it is done.
    """
    request = await _request(scope, session, request_id, REPAIRS_REQUEST)
    if request["status"] != "new" and not scope.can_machine(REPAIRS_MANAGE, request["machine_id"]):
        raise FORBIDDEN
    try:
        await requests.void(session, request, body.reason, scope.user_id, await _who(scope, session))
    except requests.RequestConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"id": request_id}


@router.get("/service-engineers", tags=["repairs"])
async def service_engineers(scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    """Who can be the executor of a request: active users with the role "service"."""
    require(scope, REPAIRS_MANAGE)
    return [
        {"id": u["id"], "name": u["full_name"] or u["login"]}
        for u in await identity.users_with_role(session, "service")
    ]


# ---------------------------------------------------------------- maintenance by running hours


class PlanIn(BaseModel):
    machine_id: int
    name: str = Field(min_length=2, max_length=200)
    interval_run_hours: float = Field(gt=0, le=100_000)
    counted_from: datetime | None = None  # e.g. the last maintenance before the system; default now
    notes: str | None = Field(default=None, max_length=2000)


class PlanUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    interval_run_hours: float = Field(gt=0, le=100_000)
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool = True


class RecordIn(BaseModel):
    performed_at: datetime | None = None
    engineer: str = Field(default="", max_length=200)
    comment: str | None = Field(default=None, max_length=2000)


async def _plan(scope: Scope, session: Session, plan_id: int, permission: str) -> dict[str, Any]:
    plan = await maintenance.get_plan(session, plan_id)
    if plan is None:
        raise NOT_FOUND
    require_machine(scope, plan["machine_id"], permission)
    return plan


@router.get("/maintenance", tags=["maintenance"])
async def maintenance_schedule(
    session: Session, machines: RepairsMachines, inactive: bool = False
) -> list[dict[str, Any]]:
    """Maintenance plans of the caller's machines: RUN hours since the last work, remaining, forecast date."""
    plans = await maintenance.schedule(session, [m["id"] for m in machines], datetime.now(UTC), inactive)
    return await _with_machines(session, plans)


@router.post("/maintenance-plans", tags=["maintenance"], status_code=status.HTTP_201_CREATED)
async def create_plan(body: PlanIn, scope: CurrentScope, session: Session) -> dict[str, int]:
    """A plan "every N hours of RUN" for a machine."""
    require_machine(scope, body.machine_id, REPAIRS_MANAGE)
    counted_from = _not_future(body.counted_from, datetime.now(UTC), "the start of counting")
    plan_id = await maintenance.create_plan(
        session, body.machine_id, body.name, body.interval_run_hours, counted_from, body.notes, scope.user_id
    )
    return {"id": plan_id}


@router.put("/maintenance-plans/{plan_id}", tags=["maintenance"])
async def update_plan(plan_id: int, body: PlanUpdate, scope: CurrentScope, session: Session) -> dict[str, int]:
    """Rename, change the interval, or switch a plan off (its records are kept)."""
    await _plan(scope, session, plan_id, REPAIRS_MANAGE)
    await maintenance.update_plan(session, plan_id, body.name, body.interval_run_hours, body.notes, body.is_active)
    return {"id": plan_id}


@router.post("/maintenance-plans/{plan_id}/records", tags=["maintenance"], status_code=status.HTTP_201_CREATED)
async def add_record(plan_id: int, body: RecordIn, scope: CurrentScope, session: Session) -> dict[str, int]:
    """The work of the plan is done: running hours count again from this moment."""
    await _plan(scope, session, plan_id, REPAIRS_MANAGE)
    performed_at = _not_future(body.performed_at, datetime.now(UTC), "the work")
    engineer = body.engineer.strip() or await _who(scope, session)
    return {"id": await maintenance.add_record(session, plan_id, performed_at, engineer, body.comment, scope.user_id)}


@router.get("/maintenance-plans/{plan_id}/records", tags=["maintenance"])
async def list_records(plan_id: int, scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    await _plan(scope, session, plan_id, REPAIRS_VIEW)
    return await maintenance.list_records(session, plan_id)


@router.post("/maintenance-records/{record_id}/void", tags=["maintenance"])
async def void_record(record_id: int, body: VoidBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """A record entered by mistake is voided with a reason; the plan counts again from the previous one."""
    record = await maintenance.get_record(session, record_id)
    if record is None:
        raise NOT_FOUND
    require_machine(scope, record["machine_id"], REPAIRS_MANAGE)
    if record["voided_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the record is already voided")
    await maintenance.void_record(session, record_id, body.reason)
    return {"id": record_id}
