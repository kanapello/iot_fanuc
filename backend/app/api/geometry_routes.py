"""Geometric accuracy API: the internal archive of the service (ADR 0005).

Only the service (and the superadmin) hold geometry rights. For everybody else every route here
answers 404, even for their own machines: the archive does not exist for customers. The one
exception is marking a collision, which operators and masters do from the shop floor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.admin import journal
from app.admin import service as admin
from app.api.deps import FORBIDDEN, NOT_FOUND, CurrentScope, Session, SettingsDep, as_utc, require_machine
from app.core.access import GEOMETRY_EDIT, GEOMETRY_VIEW, OPERATOR_INPUT, Scope
from app.geometry import service as geometry
from app.geometry.catalog import ARCS, CATALOG, DIRECTIONS, PLANE_AXES, PLANES

router = APIRouter(prefix="/api/v1", tags=["geometry"])
Axis = Literal["X", "Y", "Z"]
Direction = Literal["CW", "CCW"]
PlaneName = Literal["XY", "YZ", "ZX"]


def _bad_input(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


def _require_archive(scope: Scope) -> None:
    """The archive exists only for the service."""
    if not scope.has(GEOMETRY_VIEW):
        raise NOT_FOUND


def _require_geometry(scope: Scope, machine_id: int, permission: str = GEOMETRY_VIEW) -> None:
    """A machine without geometry rights has no archive for the caller: 404, not 403."""
    if not scope.can_machine(permission, machine_id):
        raise NOT_FOUND


async def _test_machine(scope: Scope, session: Session, test_id: int, permission: str) -> int:
    machine_id = await geometry.test_owner(session, test_id)
    if machine_id is None:
        raise NOT_FOUND
    _require_geometry(scope, machine_id, permission)
    return machine_id


async def _writable_test(scope: Scope, session: Session, test_id: int) -> None:
    await _test_machine(scope, session, test_id, GEOMETRY_EDIT)
    if (await geometry.get_test(session, test_id))["voided_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the test is voided")


@router.get("/geometry/catalog")
async def catalog(scope: CurrentScope) -> dict[str, Any]:
    """Planes, arcs and the parameters of a ballbar test with their units."""
    _require_archive(scope)
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


@router.get("/geometry/machines")
async def archive_index(scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    """Machines of the archive: when each was last tested and how many days ago."""
    _require_archive(scope)
    machines = await admin.list_machines(session, scope.machine_ids(GEOMETRY_VIEW))
    tested = await geometry.overview(session, [m["id"] for m in machines], datetime.now(UTC))
    return [
        {
            "machine_id": m["id"],
            "machine_name": m["name"],
            "org_name": m["org_name"],
            "site_name": m["site_name"],
            "area_name": m["area_name"],
            **tested[m["id"]],
        }
        for m in machines
    ]


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
    _require_geometry(scope, machine_id)
    return await geometry.list_tests(session, machine_id)


@router.post("/machines/{machine_id}/ballbar/tests", status_code=status.HTTP_201_CREATED)
async def create_test(machine_id: int, body: TestBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    _require_geometry(scope, machine_id, GEOMETRY_EDIT)
    try:
        return {"id": await geometry.create_test(session, machine_id, body.to_input(), scope.user_id)}
    except geometry.GeometryInputError as exc:
        raise _bad_input(exc) from exc


@router.get("/ballbar/tests/{test_id}")
async def get_test(test_id: int, scope: CurrentScope, session: Session) -> dict[str, Any]:
    await _test_machine(scope, session, test_id, GEOMETRY_VIEW)
    return await geometry.get_test(session, test_id)


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
    machine_id = await geometry.file_owner(session, file_id)
    if machine_id is None:
        raise NOT_FOUND
    _require_geometry(scope, machine_id)
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
    """Raw points of one run (angle, radial deviation from nominal) for the polar plot."""
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


# ---------------------------------------------------------------- trend


@router.get("/machines/{machine_id}/ballbar/groups")
async def trend_groups(machine_id: int, scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    """Groups of comparable tests: the same template, or without a template the same plane, radius, feed and arc."""
    _require_geometry(scope, machine_id)
    return await geometry.groups(session, machine_id)


@router.get("/machines/{machine_id}/ballbar/trend")
async def trend(
    machine_id: int,
    scope: CurrentScope,
    session: Session,
    group: Annotated[str, Query(max_length=300)],
    parameter: str,
    axis: Axis | None = None,
    direction: Direction | None = None,
) -> dict[str, Any]:
    """One parameter over the tests of a group, with collision marks and repairs as events."""
    _require_geometry(scope, machine_id)
    try:
        return await geometry.trend(session, machine_id, group, parameter, axis, direction)
    except ValueError as exc:
        raise _bad_input(exc) from exc


# ---------------------------------------------------------------- events: collisions and repairs


class CollisionBody(BaseModel):
    ts: datetime | None = None
    comment: str | None = Field(default=None, max_length=1000)
    operator: str | None = Field(default=None, max_length=200)


@router.post("/machines/{machine_id}/collisions", status_code=status.HTTP_201_CREATED)
async def mark_collision(machine_id: int, body: CollisionBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """Mark "collision": an event entered on the shop floor (operator, master) or by the service.

    Customers enter it but do not see it afterwards: the marks are shown only in the service archive.
    """
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
    """Collision marks, newest first; voided ones stay in the list with the reason."""
    _require_geometry(scope, machine_id)
    since = datetime.now(UTC) - timedelta(days=days)
    marks = await journal.list_collisions(session, machine_id, since)
    return [
        {k: m[k] for k in ("id", "ts", "comment", "operator", "created_at", "voided_at", "void_reason")}
        for m in reversed(marks)
    ]


@router.post("/collisions/{collision_id}/void")
async def void_collision(collision_id: int, body: VoidBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """A mistaken mark is voided with a reason, not deleted; it no longer shows on the trend."""
    mark = await journal.get_collision(session, collision_id)
    if mark is None:
        raise NOT_FOUND
    _require_geometry(scope, mark["machine_id"], GEOMETRY_EDIT)
    if mark["voided_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the mark is already voided")
    await journal.void_collision(session, collision_id, body.reason)
    return {"id": collision_id}


class RepairBody(BaseModel):
    completed_at: datetime
    description: str = Field(min_length=3, max_length=2000)
    engineer: str = Field(default="", max_length=200)


@router.get("/machines/{machine_id}/repairs")
async def list_repairs(machine_id: int, scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    _require_geometry(scope, machine_id)
    return await geometry.list_repairs(session, machine_id)


@router.post("/machines/{machine_id}/repairs", status_code=status.HTTP_201_CREATED)
async def add_repair(machine_id: int, body: RepairBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """Record a completed repair; it appears as an event on the trend."""
    _require_geometry(scope, machine_id, GEOMETRY_EDIT)
    completed_at = as_utc(body.completed_at)
    if completed_at > datetime.now(UTC) + timedelta(days=1):
        raise _bad_input(ValueError("the repair date is in the future"))
    engineer = body.engineer or scope.name
    return {
        "id": await geometry.add_repair(session, machine_id, completed_at, body.description, engineer, scope.user_id)
    }


@router.post("/repairs/{repair_id}/void")
async def void_repair(repair_id: int, body: VoidBody, scope: CurrentScope, session: Session) -> dict[str, int]:
    """A wrong repair record is voided with a reason, not deleted; it no longer shows on the trend."""
    repair = await geometry.get_repair(session, repair_id)
    if repair is None:
        raise NOT_FOUND
    _require_geometry(scope, repair["machine_id"], GEOMETRY_EDIT)
    if repair["voided_at"] is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the record is already voided")
    await geometry.void_repair(session, repair_id, body.reason)
    return {"id": repair_id}
