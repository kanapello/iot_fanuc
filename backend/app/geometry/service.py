"""Geometric accuracy (ballbar) data: tests, results, original files, raw points, tolerances,
next-check policy, machine status and the service worklist.

Functions that answer for several machines take the machines of the caller's access
scope as an explicit argument; single-object functions are called only after the
caller checked access to the machine the object belongs to.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from operator import itemgetter
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import journal
from app.geometry.catalog import BY_CODE, DIRECTIONS, PLANE_AXES, PLANES, check_result

__all__ = ["BY_CODE", "PLANE_AXES"]  # re-exported for the demo seed
from app.geometry.schedule import DEFAULT_POLICY, Policy, next_check
from app.geometry.tolerances import Tolerance, exceeds, pick
from app.ingest.health import alarms_since
from app.timeline.service import run_seconds_since

_TEST_COLUMNS = (
    "t.id, t.machine_id, t.performed_at, t.engineer, t.plane, t.radius_mm, t.feed_mm_min, t.arc_deg, "
    "t.air_temp_c, t.machine_temp_c, t.template, t.notes, t.results_source, t.voided_at, t.void_reason, "
    "t.created_by, t.created_at, t.updated_at"
)
MAX_TRACE_POINTS = 200_000


class GeometryInputError(ValueError):
    """Input that does not fit the catalog or the stored data."""


@dataclass(frozen=True, slots=True)
class ResultIn:
    parameter: str
    value: float
    axis: str | None = None
    direction: str | None = None


@dataclass(slots=True)
class TestIn:
    performed_at: datetime
    engineer: str
    plane: str
    radius_mm: float
    feed_mm_min: float
    arc_deg: int
    air_temp_c: float | None = None
    machine_temp_c: float | None = None
    template: str | None = None
    notes: str | None = None
    results: list[ResultIn] = field(default_factory=list)


def _result_rows(test_id: int, plane: str, results: Sequence[ResultIn]) -> list[dict[str, Any]]:
    rows, seen = [], set()
    for r in results:
        try:
            entry = check_result(plane, r.parameter, r.axis, r.direction)
        except ValueError as exc:
            raise GeometryInputError(str(exc)) from exc
        key = (r.parameter, r.axis, r.direction)
        if key in seen:
            raise GeometryInputError(f"duplicate value for {r.parameter} {r.axis or ''} {r.direction or ''}".strip())
        if not math.isfinite(r.value):
            raise GeometryInputError(f"{r.parameter}: value must be a finite number")
        seen.add(key)
        rows.append({"t": test_id, "p": r.parameter, "a": r.axis, "d": r.direction, "v": r.value, "u": entry.unit})
    return rows


async def _replace_results(session: AsyncSession, test_id: int, plane: str, results: Sequence[ResultIn]) -> None:
    rows = _result_rows(test_id, plane, results)
    await session.execute(text("DELETE FROM ballbar_results WHERE test_id = :t"), {"t": test_id})
    if rows:
        await session.execute(
            text(
                "INSERT INTO ballbar_results (test_id, parameter, axis, direction, value, unit) "
                "VALUES (:t, :p, :a, :d, :v, :u)"
            ),
            rows,
        )


def _test_params(data: TestIn) -> dict[str, Any]:
    if data.plane not in PLANES:
        raise GeometryInputError(f"unknown plane {data.plane!r}")
    return {
        "performed_at": data.performed_at,
        "engineer": data.engineer.strip(),
        "plane": data.plane,
        "radius": data.radius_mm,
        "feed": data.feed_mm_min,
        "arc": data.arc_deg,
        "air": data.air_temp_c,
        "mtemp": data.machine_temp_c,
        "template": (data.template or "").strip() or None,
        "notes": (data.notes or "").strip() or None,
    }


async def create_test(session: AsyncSession, machine_id: int, data: TestIn, user_id: int | None) -> int:
    params = _test_params(data)
    test_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO ballbar_tests (machine_id, performed_at, engineer, plane, radius_mm, feed_mm_min, "
                    "arc_deg, air_temp_c, machine_temp_c, template, notes, created_by) VALUES (:m, :performed_at, "
                    ":engineer, :plane, :radius, :feed, :arc, :air, :mtemp, :template, :notes, :user) RETURNING id"
                ),
                {**params, "m": machine_id, "user": user_id},
            )
        ).scalar_one()
    )
    await _replace_results(session, test_id, data.plane, data.results)
    return test_id


async def update_test(session: AsyncSession, test_id: int, data: TestIn) -> None:
    """Correct a manually entered test. Results are replaced as a whole; files are never touched."""
    params = _test_params(data)
    await session.execute(
        text(
            "UPDATE ballbar_tests SET performed_at = :performed_at, engineer = :engineer, plane = :plane, "
            "radius_mm = :radius, feed_mm_min = :feed, arc_deg = :arc, air_temp_c = :air, machine_temp_c = :mtemp, "
            "template = :template, notes = :notes, updated_at = now() WHERE id = :id"
        ),
        {**params, "id": test_id},
    )
    await _replace_results(session, test_id, data.plane, data.results)


async def void_test(session: AsyncSession, test_id: int, reason: str) -> None:
    """A wrong test is not deleted (its originals stay): it is excluded from trends, status and worklist."""
    await session.execute(
        text("UPDATE ballbar_tests SET voided_at = now(), void_reason = :reason, updated_at = now() WHERE id = :id"),
        {"id": test_id, "reason": reason.strip()},
    )


async def test_owner(session: AsyncSession, test_id: int) -> int | None:
    """Machine of a test (for the access check)."""
    result = await session.execute(text("SELECT machine_id FROM ballbar_tests WHERE id = :id"), {"id": test_id})
    return result.scalar_one_or_none()


async def file_owner(session: AsyncSession, file_id: int) -> tuple[int, int] | None:
    """(test id, machine id) of a stored file."""
    row = (
        await session.execute(
            text(
                "SELECT f.test_id, t.machine_id FROM ballbar_files f "
                "JOIN ballbar_tests t ON t.id = f.test_id WHERE f.id = :id"
            ),
            {"id": file_id},
        )
    ).first()
    return None if row is None else (row.test_id, row.machine_id)


# ---------------------------------------------------------------- tolerances


def _tolerance(row: Any) -> Tolerance:
    return Tolerance(
        id=row.id,
        limit=row.limit_value,
        unit=row.unit,
        parameter=row.parameter,
        machine_id=row.machine_id,
        machine_model=row.machine_model,
        plane=row.plane,
        axis=row.axis,
    )


async def load_tolerances(session: AsyncSession, machines: Collection[dict[str, Any]]) -> list[Tolerance]:
    """Tolerances that may apply to the given machines (their own and those of their models)."""
    ids = [m["id"] for m in machines]
    models = sorted({m["model"] for m in machines if m.get("model")})
    rows = await session.execute(
        text("SELECT * FROM ballbar_tolerances WHERE machine_id = ANY(:ids) OR machine_model = ANY(:models)"),
        {"ids": ids, "models": models},
    )
    return [_tolerance(r) for r in rows]


def evaluate(
    machine: dict[str, Any], plane: str, results: Sequence[dict[str, Any]], tolerances: Sequence[Tolerance]
) -> list[dict[str, Any]]:
    """Results with the tolerance in effect and whether they exceed it."""
    evaluated = []
    for r in results:
        tol = pick(tolerances, machine["id"], machine.get("model"), plane, r["parameter"], r["axis"])
        evaluated.append(
            {
                **r,
                "limit": None if tol is None else tol.limit,
                "limit_source": None if tol is None else tol.source,
                "exceeded": exceeds(r["value"], tol),
            }
        )
    return evaluated


async def tolerance_rows(
    session: AsyncSession, machine_ids: Collection[int], model: str | None = None
) -> list[dict[str, Any]]:
    """Machine rows of the given machines and every model row (models are shared by all organizations)."""
    rows = await session.execute(
        text(
            "SELECT t.*, m.name AS machine_name FROM ballbar_tolerances t LEFT JOIN machines m ON m.id = t.machine_id "
            "WHERE (t.machine_id = ANY(:ids) OR t.machine_model IS NOT NULL) "
            "AND (CAST(:model AS text) IS NULL OR t.machine_model = :model) "
            "ORDER BY t.machine_model NULLS FIRST, t.machine_id, t.parameter, t.plane NULLS FIRST, t.axis NULLS FIRST"
        ),
        {"ids": list(machine_ids), "model": model},
    )
    return [dict(r._mapping) for r in rows]


async def tolerance_row(session: AsyncSession, tolerance_id: int) -> dict[str, Any] | None:
    row = (await session.execute(text("SELECT * FROM ballbar_tolerances WHERE id = :id"), {"id": tolerance_id})).first()
    return None if row is None else dict(row._mapping)


def _check_tolerance(parameter: str, plane: str | None, axis: str | None) -> str:
    entry = BY_CODE.get(parameter)
    if entry is None:
        raise GeometryInputError(f"unknown parameter {parameter!r}")
    if plane is not None and plane not in PLANES:
        raise GeometryInputError(f"unknown plane {plane!r}")
    if axis is not None and (not entry.per_axis or (plane is not None and axis not in PLANE_AXES[plane])):
        raise GeometryInputError(f"{parameter}: axis {axis!r} is not valid here")
    return entry.unit


async def save_tolerance(
    session: AsyncSession,
    tolerance_id: int | None,
    machine_id: int | None,
    machine_model: str | None,
    parameter: str,
    plane: str | None,
    axis: str | None,
    limit_value: float,
) -> int:
    unit = _check_tolerance(parameter, plane, axis)
    if not (limit_value > 0 and math.isfinite(limit_value)):
        raise GeometryInputError("limit must be a positive number")
    params = {
        "id": tolerance_id,
        "machine": machine_id,
        "model": machine_model,
        "parameter": parameter,
        "plane": plane,
        "axis": axis,
        "limit": limit_value,
        "unit": unit,
    }
    if tolerance_id is None:
        sql = (
            "INSERT INTO ballbar_tolerances (machine_id, machine_model, parameter, plane, axis, limit_value, unit) "
            "VALUES (:machine, :model, :parameter, :plane, :axis, :limit, :unit) RETURNING id"
        )
    else:
        sql = (
            "UPDATE ballbar_tolerances SET parameter = :parameter, plane = :plane, axis = :axis, "
            "limit_value = :limit, unit = :unit, updated_at = now() WHERE id = :id RETURNING id"
        )
    return int((await session.execute(text(sql), params)).scalar_one())


async def delete_tolerance(session: AsyncSession, tolerance_id: int) -> None:
    await session.execute(text("DELETE FROM ballbar_tolerances WHERE id = :id"), {"id": tolerance_id})


async def models_in_use(session: AsyncSession, machine_ids: Collection[int]) -> list[str]:
    """Models of the given machines and models that already have tolerances."""
    rows = await session.execute(
        text(
            "SELECT model FROM machines WHERE id = ANY(:ids) AND model IS NOT NULL "
            "UNION SELECT machine_model FROM ballbar_tolerances WHERE machine_model IS NOT NULL ORDER BY 1"
        ),
        {"ids": list(machine_ids)},
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------- tests and their data


async def _results(session: AsyncSession, test_ids: Collection[int]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {t: [] for t in test_ids}
    if not test_ids:
        return result
    rows = await session.execute(
        text(
            "SELECT test_id, parameter, axis, direction, value, unit FROM ballbar_results "
            "WHERE test_id = ANY(:ids) ORDER BY test_id, parameter, axis NULLS FIRST, direction NULLS FIRST"
        ),
        {"ids": list(test_ids)},
    )
    for r in rows:
        result[r.test_id].append(
            {"parameter": r.parameter, "axis": r.axis, "direction": r.direction, "value": r.value, "unit": r.unit}
        )
    return result


async def list_tests(session: AsyncSession, machine: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        dict(r._mapping)
        for r in await session.execute(
            text(
                f"SELECT {_TEST_COLUMNS}, (SELECT count(*) FROM ballbar_files f WHERE f.test_id = t.id) AS files, "
                "EXISTS (SELECT 1 FROM ballbar_traces p WHERE p.test_id = t.id) AS has_traces "
                "FROM ballbar_tests t WHERE t.machine_id = :m ORDER BY t.performed_at DESC, t.id DESC"
            ),
            {"m": machine["id"]},
        )
    ]
    results = await _results(session, [r["id"] for r in rows])
    tolerances = await load_tolerances(session, [machine])
    for row in rows:
        evaluated = evaluate(machine, row["plane"], results[row["id"]], tolerances)
        row["results_count"] = len(evaluated)
        row["exceeded"] = [r for r in evaluated if r["exceeded"]]
    return rows


async def get_test(session: AsyncSession, machine: dict[str, Any], test_id: int) -> dict[str, Any]:
    row = (
        await session.execute(text(f"SELECT {_TEST_COLUMNS} FROM ballbar_tests t WHERE t.id = :id"), {"id": test_id})
    ).one()
    test = dict(row._mapping)
    tolerances = await load_tolerances(session, [machine])
    test["results"] = evaluate(machine, test["plane"], (await _results(session, [test_id]))[test_id], tolerances)
    test["files"] = [
        dict(r._mapping)
        for r in await session.execute(
            text(
                "SELECT id, kind, filename, content_type, size_bytes, sha256, uploaded_by_name, uploaded_at "
                "FROM ballbar_files WHERE test_id = :id ORDER BY uploaded_at, id"
            ),
            {"id": test_id},
        )
    ]
    test["traces"] = [
        dict(r._mapping)
        for r in await session.execute(
            text(
                "SELECT direction, cardinality(angle_deg) AS points, sample_rate_hz, source FROM ballbar_traces "
                "WHERE test_id = :id ORDER BY direction"
            ),
            {"id": test_id},
        )
    ]
    return test


def file_kind(filename: str, content: bytes) -> tuple[str, str]:
    """(kind, content type) of an uploaded original; only Ballbar 20 results and PDF reports are accepted."""
    name = filename.lower()
    if name.endswith(".b5r"):
        return "b5r", "application/octet-stream"
    if name.endswith(".pdf"):
        if not content.startswith(b"%PDF-"):
            raise GeometryInputError("the file is not a PDF document")
        return "pdf", "application/pdf"
    raise GeometryInputError("only .b5r and .pdf files are accepted")


async def add_file(
    session: AsyncSession, test_id: int, filename: str, content: bytes, user_id: int | None, user_name: str
) -> dict[str, Any]:
    """Store an original file. Files are append-only: a corrected report is uploaded as one more file."""
    if not content:
        raise GeometryInputError("the file is empty")
    kind, content_type = file_kind(filename, content)
    row = (
        await session.execute(
            text(
                "INSERT INTO ballbar_files (test_id, kind, filename, content_type, size_bytes, sha256, content, "
                "uploaded_by, uploaded_by_name) "
                "VALUES (:t, :kind, :name, :ctype, :size, :sha, :content, :user, :uname) "
                "RETURNING id, kind, filename, content_type, size_bytes, sha256, uploaded_by_name, uploaded_at"
            ),
            {
                "t": test_id,
                "kind": kind,
                "name": filename,
                "ctype": content_type,
                "size": len(content),
                "sha": hashlib.sha256(content).hexdigest(),
                "content": content,
                "user": user_id,
                "uname": user_name,
            },
        )
    ).one()
    return dict(row._mapping)


async def read_file(session: AsyncSession, file_id: int) -> dict[str, Any]:
    row = (
        await session.execute(
            text("SELECT filename, content_type, content FROM ballbar_files WHERE id = :id"), {"id": file_id}
        )
    ).one()
    return dict(row._mapping)


async def put_trace(
    session: AsyncSession,
    test_id: int,
    direction: str,
    angles: Sequence[float],
    deviations: Sequence[float],
    sample_rate_hz: float | None,
    source: str = "import",
) -> int:
    """Store raw points of one run: polar angle (degrees) and radial deviation from nominal (µm)."""
    if direction not in DIRECTIONS:
        raise GeometryInputError(f"unknown direction {direction!r}")
    if len(angles) != len(deviations):
        raise GeometryInputError("angles and deviations must have the same length")
    if not 3 <= len(angles) <= MAX_TRACE_POINTS:
        raise GeometryInputError(f"a run must have from 3 to {MAX_TRACE_POINTS} points")
    if not all(math.isfinite(v) for v in (*angles, *deviations)):
        raise GeometryInputError("points must be finite numbers")
    if any(abs(a) > 1080 for a in angles):
        raise GeometryInputError("angles are expected in degrees")
    await session.execute(
        text(
            "INSERT INTO ballbar_traces (test_id, direction, angle_deg, deviation_um, sample_rate_hz, source) "
            "VALUES (:t, :d, :a, :v, :rate, :source) ON CONFLICT (test_id, direction) DO UPDATE SET "
            "angle_deg = EXCLUDED.angle_deg, deviation_um = EXCLUDED.deviation_um, "
            "sample_rate_hz = EXCLUDED.sample_rate_hz, source = EXCLUDED.source, created_at = now()"
        ),
        {
            "t": test_id,
            "d": direction,
            "a": list(angles),
            "v": list(deviations),
            "rate": sample_rate_hz,
            "source": source,
        },
    )
    return len(angles)


async def get_traces(session: AsyncSession, test_id: int, max_points: int) -> list[dict[str, Any]]:
    """Raw points for a plot, thinned out evenly to at most ``max_points`` per run."""
    rows = await session.execute(
        text(
            "SELECT direction, angle_deg, deviation_um, sample_rate_hz, source FROM ballbar_traces "
            "WHERE test_id = :id ORDER BY direction"
        ),
        {"id": test_id},
    )
    traces = []
    for r in rows:
        step = max(1, math.ceil(len(r.angle_deg) / max(1, max_points)))
        traces.append(
            {
                "direction": r.direction,
                "points": len(r.angle_deg),
                "angle_deg": list(r.angle_deg[::step]),
                "deviation_um": list(r.deviation_um[::step]),
                "sample_rate_hz": r.sample_rate_hz,
                "source": r.source,
            }
        )
    return traces


# ---------------------------------------------------------------- trend


async def configurations(session: AsyncSession, machine_id: int) -> list[dict[str, Any]]:
    """Test set-ups of a machine that can be compared with each other: same plane, radius and feed."""
    rows = await session.execute(
        text(
            "SELECT plane, radius_mm, feed_mm_min, count(*) AS tests, max(performed_at) AS last FROM ballbar_tests "
            "WHERE machine_id = :m AND voided_at IS NULL GROUP BY plane, radius_mm, feed_mm_min ORDER BY last DESC"
        ),
        {"m": machine_id},
    )
    return [dict(r._mapping) for r in rows]


async def trend(
    session: AsyncSession,
    machine: dict[str, Any],
    parameter: str,
    plane: str,
    radius_mm: float,
    feed_mm_min: float,
    axis: str | None,
    direction: str | None,
) -> dict[str, Any]:
    """Values of one parameter over time, only from tests with the same plane, radius and feed."""
    entry = check_result(plane, parameter, axis, direction)
    rows = await session.execute(
        text(
            "SELECT t.id AS test_id, t.performed_at, r.value FROM ballbar_tests t "
            "JOIN ballbar_results r ON r.test_id = t.id AND r.parameter = :p "
            "AND r.axis IS NOT DISTINCT FROM CAST(:a AS text) AND r.direction IS NOT DISTINCT FROM CAST(:d AS text) "
            "WHERE t.machine_id = :m AND t.voided_at IS NULL AND t.plane = :plane "
            "AND t.radius_mm = :radius AND t.feed_mm_min = :feed ORDER BY t.performed_at, t.id"
        ),
        {
            "m": machine["id"],
            "p": parameter,
            "a": axis,
            "d": direction,
            "plane": plane,
            "radius": radius_mm,
            "feed": feed_mm_min,
        },
    )
    tol = pick(await load_tolerances(session, [machine]), machine["id"], machine.get("model"), plane, parameter, axis)
    points = [
        {"test_id": r.test_id, "performed_at": r.performed_at, "value": r.value, "exceeded": exceeds(r.value, tol)}
        for r in rows
    ]
    return {
        "parameter": parameter,
        "unit": entry.unit,
        "limit": None if tol is None else tol.limit,
        "limit_source": None if tol is None else tol.source,
        "points": points,
    }


# ---------------------------------------------------------------- policy, status, worklist


def _policy(row: Any) -> Policy:
    return Policy(
        interval_days=row.interval_days,
        interval_run_hours=row.interval_run_hours,
        on_alarm=row.on_alarm,
        on_collision=row.on_collision,
        alarm_patterns=tuple(row.alarm_patterns),
    )


async def policies(session: AsyncSession, machine_ids: Collection[int]) -> dict[int, Policy]:
    rows = await session.execute(
        text("SELECT * FROM ballbar_policies WHERE machine_id = ANY(:ids)"), {"ids": list(machine_ids)}
    )
    found = {r.machine_id: _policy(r) for r in rows}
    return {m: found.get(m, DEFAULT_POLICY) for m in machine_ids}


async def set_policy(session: AsyncSession, machine_id: int, policy: Policy) -> None:
    if (
        policy.interval_days is None
        and policy.interval_run_hours is None
        and not policy.on_alarm
        and not policy.on_collision
    ):
        raise GeometryInputError("at least one reason for the next check is required")
    await session.execute(
        text(
            "INSERT INTO ballbar_policies (machine_id, interval_days, interval_run_hours, on_alarm, on_collision, "
            "alarm_patterns) VALUES (:m, :days, :hours, :alarm, :collision, :patterns) ON CONFLICT (machine_id) "
            "DO UPDATE SET interval_days = EXCLUDED.interval_days, interval_run_hours = EXCLUDED.interval_run_hours, "
            "on_alarm = EXCLUDED.on_alarm, on_collision = EXCLUDED.on_collision, "
            "alarm_patterns = EXCLUDED.alarm_patterns, updated_at = now()"
        ),
        {
            "m": machine_id,
            "days": policy.interval_days,
            "hours": policy.interval_run_hours,
            "alarm": policy.on_alarm,
            "collision": policy.on_collision,
            "patterns": [p.strip() for p in policy.alarm_patterns if p.strip()],
        },
    )


async def _latest_tests(session: AsyncSession, machine_ids: Collection[int]) -> list[dict[str, Any]]:
    """The latest valid test of every machine in every plane."""
    rows = await session.execute(
        text(
            f"SELECT DISTINCT ON (t.machine_id, t.plane) {_TEST_COLUMNS} FROM ballbar_tests t "
            "WHERE t.machine_id = ANY(:ids) AND t.voided_at IS NULL "
            "ORDER BY t.machine_id, t.plane, t.performed_at DESC, t.id DESC"
        ),
        {"ids": list(machine_ids)},
    )
    return [dict(r._mapping) for r in rows]


async def machine_states(
    session: AsyncSession, machines: Sequence[dict[str, Any]], now: datetime
) -> dict[int, dict[str, Any]]:
    """For every machine: next check and the latest test of every plane with its exceedances."""
    ids = [m["id"] for m in machines]
    latest = await _latest_tests(session, ids)
    results = await _results(session, [t["id"] for t in latest])
    tolerances = await load_tolerances(session, machines)
    by_id = {m["id"]: m for m in machines}
    last_at: dict[int, datetime] = {}
    planes: dict[int, list[dict[str, Any]]] = {m: [] for m in ids}
    for test in latest:
        machine = by_id[test["machine_id"]]
        evaluated = evaluate(machine, test["plane"], results[test["id"]], tolerances)
        planes[machine["id"]].append({**test, "exceeded": [r for r in evaluated if r["exceeded"]]})
        last_at[machine["id"]] = max(test["performed_at"], last_at.get(machine["id"], test["performed_at"]))

    policy_of = await policies(session, ids)
    run_seconds = await run_seconds_since(session, last_at, now)
    alarms = await alarms_since(session, last_at)
    collisions = await journal.list_collisions(session, last_at)
    states: dict[int, dict[str, Any]] = {}
    for machine_id in ids:
        check = next_check(
            policy_of[machine_id],
            last_at.get(machine_id),
            run_seconds.get(machine_id),
            alarms.get(machine_id, []),
            [(ts, comment) for ts, comment, _id, _op in collisions.get(machine_id, [])],
            now,
        )
        policy = policy_of[machine_id]
        states[machine_id] = {
            "next_check": check.as_dict(),
            "policy": {
                "interval_days": policy.interval_days,
                "interval_run_hours": policy.interval_run_hours,
                "on_alarm": policy.on_alarm,
                "on_collision": policy.on_collision,
                "alarm_patterns": list(policy.alarm_patterns),
                "is_default": policy.is_default,
            },
            "planes": sorted(planes[machine_id], key=itemgetter("plane")),
        }
    return states


SEVERITY = {"high": 0, "medium": 1, "low": 2}


async def worklist(session: AsyncSession, machines: Sequence[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """What the service has to do: exceeded tolerances and due checks of the given machines."""
    states = await machine_states(session, machines, now)
    items = []
    for machine in machines:
        state = states[machine["id"]]
        head = {
            "machine_id": machine["id"],
            "machine_name": machine["name"],
            "machine_model": machine.get("model"),
            "org_name": machine["org_name"],
            "site_name": machine["site_name"],
            "area_name": machine["area_name"],
        }
        for test in state["planes"]:
            if test["exceeded"]:
                items.append(
                    {
                        **head,
                        "kind": "tolerance",
                        "severity": "high",
                        "at": test["performed_at"],
                        "test_id": test["id"],
                        "plane": test["plane"],
                        "details": test["exceeded"],
                    }
                )
        check = state["next_check"]
        status = check["status"]
        if status in ("event", "overdue", "soon", "never"):
            severity = {"event": "high", "overdue": "high", "soon": "medium", "never": "low"}[status]
            at = check["triggers"][0]["ts"] if status == "event" else check["due_date"] or check["last_test_at"]
            items.append({**head, "kind": f"check_{status}", "severity": severity, "at": at, "next_check": check})
    items.sort(key=lambda i: (SEVERITY[i["severity"]], i["at"] is None, i["at"] or now))
    return items
