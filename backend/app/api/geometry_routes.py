"""Geometric accuracy API: ballbar tests in the machine card, tolerances, next check, service worklist."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.admin import journal
from app.admin import service as admin
from app.api.deps import FORBIDDEN, NOT_FOUND, CurrentScope, Session, SettingsDep, as_utc, require, require_machine
from app.core.access import GEOMETRY_EDIT, GEOMETRY_VIEW, MACHINE_VIEW, OPERATOR_INPUT, Scope
from app.geometry import service as geometry
from app.geometry.catalog import ARCS, CATALOG, DIRECTIONS, PLANE_AXES, PLANES
from app.geometry.schedule import Policy

router = APIRouter(prefix="/api/v1", tags=["geometry"])
Axis = Literal["X", "Y", "Z"]
Direction = Literal["CW", "CCW"]
PlaneName = Literal["XY", "YZ", "ZX"]


def _bad_input(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


async def _machine(scope: Scope, session: Session, machine_id: int, permission: str) -> dict[str, Any]:
    require_machine(scope, machine_id, permission)
    (machine,) = await admin.list_machines(session, [machine_id], active_only=False)
    return machine


async def _test_machine(scope: Scope, session: Session, test_id: int, permission: str) -> dict[str, Any]:
    machine_id = await geometry.test_owner(session, test_id)
    if machine_id is None:
        raise NOT_FOUND
    return await _machine(scope, session, machine_id, permission)


async def _writable_test(scope: Scope, session: Session, test_id: int) -> dict[str, Any]:
    machine = await _test_machine(scope, session, test_id, GEOMETRY_EDIT)
    test = await geometry.get_test(session, machine, test_id)
    if test["voided_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the test is voided")
    return machine


@router.get("/geometry/catalog")
async def catalog(_: CurrentScope) -> dict[str, Any]:
    """Planes, arcs and the parameters of a ballbar test with their units."""
    return {
        "planes": list(PLANES),
        "plane_axes": {plane: list(axes) for plane, axes in PLANE_AXES.items()},
        "arcs": list(ARCS),
        "directions": list(DIRECTIONS),
        "parameters": [
            {
                "code": p.code,
                "name": p.name,
                "unit": p.unit,
                "group": p.group,
                "per_axis": p.per_axis,
                "directions": list(p.directions),
            }
            for p in CATALOG
        ],
    }


# ---------------------------------------------------------------- tests


class ResultBody(BaseModel):
    parameter: str
    axis: Axis | None = None
    direction: Direction | None = None
    value: float


class TestBody(BaseModel):
    performed_at: datetime
    engineer: str = Field(min_length=1, max_length=200)
    plane: PlaneName
    radius_mm: float = Field(gt=0, le=2000)
    feed_mm_min: float = Field(gt=0, le=100_000)
    arc_deg: Literal[360, 220]
    air_temp_c: float | None = Field(default=None, ge=-50, le=100)
    machine_temp_c: float | None = Field(default=None, ge=-50, le=100)
    template: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    results: list[ResultBody] = Field(default_factory=list, max_length=200)

    def to_input(self) -> geometry.TestIn:
        performed_at = as_utc(self.performed_at)
        if performed_at > datetime.now(UTC) + timedelta(days=1):
            raise _bad_input(ValueError("the test date is in the future"))
        return geometry.TestIn(
            performed_at=performed_at,
            engineer=self.engineer,
            plane=self.plane,
            radius_mm=self.radius_mm,
            feed_mm_min=self.feed_mm_min,
            arc_deg=self.arc_deg,
            air_temp_c=self.air_temp_c,
            machine_temp_c=self.machine_temp_c,
            template=self.template,
            notes=self.notes,
            results=[geometry.ResultIn(r.parameter, r.value, r.axis, r.direction) for r in self.results],
        )


@router.get("/machines/{machine_id}/ballbar/tests")
async def list_tests(machine_id: int, scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    machine = await _machine(scope, session, machine_id, GEOMETRY_VIEW)
    return await geometry.list_tests(session, machine)


@router.post("/machines/{machine_id}/ballbar/tests", status_code=status.HTTP_201_CREATED)
async def create_test(machine_id: int, body: TestBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    await _machine(scope, session, machine_id, GEOMETRY_EDIT)
    try:
        return {"id": await geometry.create_test(session, machine_id, body.to_input(), scope.user_id)}
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc


@router.get("/ballbar/tests/{test_id}")
async def get_test(test_id: int, scope: CurrentScope, session: Session) -> dict[str, Any]:
    machine = await _test_machine(scope, session, test_id, GEOMETRY_VIEW)
    return await geometry.get_test(session, machine, test_id)


@router.put("/ballbar/tests/{test_id}")
async def update_test(test_id: int, body: TestBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    await _writable_test(scope, session, test_id)
    try:
        await geometry.update_test(session, test_id, body.to_input())
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc
    return {"id": test_id}


class VoidBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/ballbar/tests/{test_id}/void")
async def void_test(test_id: int, body: VoidBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """A mistaken test is voided, not deleted: its original files are kept forever."""
    await _writable_test(scope, session, test_id)
    await geometry.void_test(session, test_id, body.reason)
    return {"id": test_id}


@router.post("/ballbar/tests/{test_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    test_id: int,
    scope: CurrentScope,
    session: Session,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Upload the original .b5r or the PDF report. Stored forever, never replaced or deleted."""
    await _writable_test(scope, session, test_id)
    limit = settings.geometry_max_file_mb * 1024 * 1024
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"files up to {settings.geometry_max_file_mb} MB")
    try:
        return await geometry.add_file(session, test_id, file.filename or "file", content, scope.user_id, scope.name)
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc


@router.get("/ballbar/files/{file_id}")
async def download_file(file_id: int, scope: CurrentScope, session: Session) -> Response:
    owner = await geometry.file_owner(session, file_id)
    if owner is None:
        raise NOT_FOUND
    await _machine(scope, session, owner[1], GEOMETRY_VIEW)
    stored = await geometry.read_file(session, file_id)
    disposition = f"attachment; filename*=UTF-8''{quote(stored['filename'])}"
    return Response(stored["content"], media_type=stored["content_type"], headers={"Content-Disposition": disposition})


class TraceBody(BaseModel):
    direction: Direction
    angle_deg: list[float] = Field(max_length=geometry.MAX_TRACE_POINTS)
    deviation_um: list[float] = Field(max_length=geometry.MAX_TRACE_POINTS)
    sample_rate_hz: float | None = Field(default=None, gt=0)


@router.put("/ballbar/tests/{test_id}/traces")
async def put_trace(test_id: int, body: TraceBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """Raw points of one run (angle, radial deviation from nominal). The future .b5r parser writes here too."""
    await _writable_test(scope, session, test_id)
    try:
        points = await geometry.put_trace(
            session, test_id, body.direction, body.angle_deg, body.deviation_um, body.sample_rate_hz
        )
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc
    return {"points": points}


@router.get("/ballbar/tests/{test_id}/traces")
async def get_traces(
    test_id: int, scope: CurrentScope, session: Session, max_points: Annotated[int, Query(ge=10, le=20_000)] = 2000
) -> list[dict[str, Any]]:
    await _test_machine(scope, session, test_id, GEOMETRY_VIEW)
    return await geometry.get_traces(session, test_id, max_points)


# ---------------------------------------------------------------- machine card: status, trend, policy, collisions


@router.get("/machines/{machine_id}/ballbar/status")
async def machine_status(machine_id: int, scope: CurrentScope, session: Session) -> dict[str, Any]:
    """Next check (by date, running hours, events) and the latest test of every plane."""
    machine = await _machine(scope, session, machine_id, GEOMETRY_VIEW)
    state = (await geometry.machine_states(session, [machine], datetime.now(UTC)))[machine_id]
    return {**state, "configurations": await geometry.configurations(session, machine_id)}


@router.get("/machines/{machine_id}/ballbar/trend")
async def trend(
    machine_id: int,
    scope: CurrentScope,
    session: Session,
    parameter: str,
    plane: PlaneName,
    radius_mm: float,
    feed_mm_min: float,
    axis: Axis | None = None,
    direction: Direction | None = None,
) -> dict[str, Any]:
    """One parameter over time, only from tests with the same plane, radius and feed."""
    machine = await _machine(scope, session, machine_id, GEOMETRY_VIEW)
    try:
        return await geometry.trend(session, machine, parameter, plane, radius_mm, feed_mm_min, axis, direction)
    except ValueError as exc:
        raise _bad_input(exc) from exc


class PolicyBody(BaseModel):
    interval_days: int | None = Field(default=None, gt=0, le=3650)
    interval_run_hours: float | None = Field(default=None, gt=0, le=100_000)
    on_alarm: bool = True
    on_collision: bool = True
    alarm_patterns: list[str] = Field(default_factory=lambda: list(Policy().alarm_patterns), max_length=50)


@router.put("/machines/{machine_id}/ballbar/policy")
async def set_policy(machine_id: int, body: PolicyBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    await _machine(scope, session, machine_id, GEOMETRY_EDIT)
    policy = Policy(
        body.interval_days, body.interval_run_hours, body.on_alarm, body.on_collision, tuple(body.alarm_patterns)
    )
    try:
        await geometry.set_policy(session, machine_id, policy)
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc
    return {"machine_id": machine_id}


class CollisionBody(BaseModel):
    ts: datetime | None = None
    comment: str | None = Field(default=None, max_length=1000)
    operator: str | None = Field(default=None, max_length=200)


@router.post("/machines/{machine_id}/collisions", status_code=status.HTTP_201_CREATED)
async def mark_collision(machine_id: int, body: CollisionBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """Mark "collision" (operator, or the engineer / service after a crash): the next geometric check is due at once."""
    require_machine(scope, machine_id)
    if not (scope.can_machine(OPERATOR_INPUT, machine_id) or scope.can_machine(GEOMETRY_EDIT, machine_id)):
        raise FORBIDDEN
    now = datetime.now(UTC)
    ts = as_utc(body.ts) if body.ts else now
    if ts > now + timedelta(minutes=5):
        raise _bad_input(ValueError("the collision time is in the future"))
    return {"id": await journal.add_collision(session, machine_id, ts, body.comment, body.operator)}


@router.get("/machines/{machine_id}/collisions")
async def list_collisions(
    machine_id: int, scope: CurrentScope, session: Session, days: Annotated[int, Query(ge=1, le=3650)] = 365
) -> list[dict[str, Any]]:
    await _machine(scope, session, machine_id, GEOMETRY_VIEW)
    since = datetime.now(UTC) - timedelta(days=days)
    marks = (await journal.list_collisions(session, {machine_id: since}))[machine_id]
    return [{"id": i, "ts": ts, "comment": comment, "operator": op} for ts, comment, i, op in reversed(marks)]


# ---------------------------------------------------------------- tolerances


class ToleranceBody(BaseModel):
    machine_id: int | None = None
    machine_model: str | None = Field(default=None, max_length=100)
    parameter: str
    plane: PlaneName | None = None
    axis: Axis | None = None
    limit_value: float = Field(gt=0)


def _tolerance_access(scope: Scope, row: dict[str, Any], for_edit: bool) -> None:
    """Machine rows belong to the machine's organization; model rows are shared and edited by the service."""
    if row.get("machine_id") is not None:
        require_machine(scope, row["machine_id"], GEOMETRY_EDIT if for_edit else GEOMETRY_VIEW)
    elif (for_edit and not scope.global_authority(GEOMETRY_EDIT)) or not scope.has(GEOMETRY_VIEW):
        raise FORBIDDEN


@router.get("/geometry/tolerances")
async def list_tolerances(scope: CurrentScope, session: Session, model: str | None = None) -> dict[str, Any]:
    """Tolerances of machine models (shared) and of the machines in the caller's scope."""
    require(scope, GEOMETRY_VIEW)
    machine_ids = scope.machine_ids(GEOMETRY_VIEW)
    rows = await geometry.tolerance_rows(session, machine_ids, model)
    editable_ids = scope.machine_ids(GEOMETRY_EDIT)
    models_editable = scope.global_authority(GEOMETRY_EDIT)
    return {
        "models": await geometry.models_in_use(session, machine_ids),
        "models_editable": models_editable,
        "rows": [
            {**r, "editable": r["machine_id"] in editable_ids if r["machine_id"] is not None else models_editable}
            for r in rows
        ],
    }


@router.post("/geometry/tolerances", status_code=status.HTTP_201_CREATED)
async def create_tolerance(body: ToleranceBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    model = (body.machine_model or "").strip() or None
    if (body.machine_id is None) == (model is None):
        raise _bad_input(ValueError("exactly one of machine_id, machine_model is required"))
    _tolerance_access(scope, {"machine_id": body.machine_id}, for_edit=True)
    try:
        async with session.begin_nested():
            new_id = await geometry.save_tolerance(
                session, None, body.machine_id, model, body.parameter, body.plane, body.axis, body.limit_value
            )
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "such a tolerance already exists") from exc
    return {"id": new_id}


async def _tolerance(scope: Scope, session: Session, tolerance_id: int, for_edit: bool) -> dict[str, Any]:
    row = await geometry.tolerance_row(session, tolerance_id)
    if row is None:
        raise NOT_FOUND
    if row["machine_id"] is not None and not scope.can_machine(MACHINE_VIEW, row["machine_id"]):
        raise NOT_FOUND
    _tolerance_access(scope, row, for_edit)
    return row


@router.put("/geometry/tolerances/{tolerance_id}")
async def update_tolerance(
    tolerance_id: int, body: ToleranceBody, scope: CurrentScope, session: Session
) -> dict[str, int]:
    """Change parameter, plane, axis or limit; the owner of a row (machine or model) does not change."""
    row = await _tolerance(scope, session, tolerance_id, for_edit=True)
    try:
        async with session.begin_nested():
            await geometry.save_tolerance(
                session, tolerance_id, row["machine_id"], row["machine_model"], body.parameter, body.plane, body.axis,
                body.limit_value,
            )  # fmt: skip
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "such a tolerance already exists") from exc
    return {"id": tolerance_id}


@router.delete("/geometry/tolerances/{tolerance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tolerance(tolerance_id: int, scope: CurrentScope, session: Session) -> None:
    await _tolerance(scope, session, tolerance_id, for_edit=True)
    await geometry.delete_tolerance(session, tolerance_id)


# ---------------------------------------------------------------- service screen


@router.get("/service/worklist")
async def worklist(scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    """Exceeded tolerances and due geometric checks of every machine in the caller's scope."""
    require(scope, GEOMETRY_VIEW)
    machines = await admin.list_machines(session, scope.machine_ids(GEOMETRY_VIEW))
    return await geometry.worklist(session, machines, datetime.now(UTC))
