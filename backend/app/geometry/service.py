"""Internal archive of the service: ballbar tests, results, original files, raw circle points,
parameter trends and the repair log.

There is no analysis here: no tolerances, no statuses, no next-check calculation (ADR 0005).
Functions that answer for several machines take the machines of the caller's access scope as an
explicit argument; single-object functions are called only after the caller checked access to the
machine the object belongs to.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import journal
from app.geometry.catalog import DIRECTIONS, PLANES, check_result
from app.repairs import records

_TEST_COLUMNS = (
    "t.id, t.machine_id, t.performed_at, t.engineer, t.plane, t.radius_mm, t.feed_mm_min, t.arc_deg, "
    "t.air_temp_c, t.machine_temp_c, t.template, t.notes, t.voided_at, t.void_reason, "
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


# ---------------------------------------------------------------- tests and results


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
    """A wrong test is not deleted (its originals stay): it is left out of the trend."""
    await session.execute(
        text("UPDATE ballbar_tests SET voided_at = now(), void_reason = :reason, updated_at = now() WHERE id = :id"),
        {"id": test_id, "reason": reason.strip()},
    )


async def test_owner(session: AsyncSession, test_id: int) -> int | None:
    """Machine of a test (for the access check)."""
    result = await session.execute(text("SELECT machine_id FROM ballbar_tests WHERE id = :id"), {"id": test_id})
    return result.scalar_one_or_none()


async def file_owner(session: AsyncSession, file_id: int) -> int | None:
    """Machine of a stored file (for the access check)."""
    result = await session.execute(
        text("SELECT t.machine_id FROM ballbar_files f JOIN ballbar_tests t ON t.id = f.test_id WHERE f.id = :id"),
        {"id": file_id},
    )
    return result.scalar_one_or_none()


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


async def list_tests(session: AsyncSession, machine_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            f"SELECT {_TEST_COLUMNS}, (SELECT count(*) FROM ballbar_files f WHERE f.test_id = t.id) AS files, "
            "(SELECT count(*) FROM ballbar_results r WHERE r.test_id = t.id) AS results_count, "
            "EXISTS (SELECT 1 FROM ballbar_traces p WHERE p.test_id = t.id) AS has_traces "
            "FROM ballbar_tests t WHERE t.machine_id = :m ORDER BY t.performed_at DESC, t.id DESC"
        ),
        {"m": machine_id},
    )
    return [dict(r._mapping) for r in rows]


async def get_test(session: AsyncSession, test_id: int) -> dict[str, Any]:
    row = (
        await session.execute(text(f"SELECT {_TEST_COLUMNS} FROM ballbar_tests t WHERE t.id = :id"), {"id": test_id})
    ).one()
    test = dict(row._mapping)
    test["results"] = (await _results(session, [test_id]))[test_id]
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


# ---------------------------------------------------------------- original files and raw points


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


def group_key(template: str | None, plane: str, radius_mm: float, feed_mm_min: float, arc_deg: int) -> str:
    """Which tests are comparable: the same template if one is set, otherwise the same set-up.

    Templates are compared ignoring case and surrounding spaces.
    """
    name = (template or "").strip().lower()
    if name:
        return f"template:{name}"
    return f"setup:{plane}|{radius_mm:g}|{feed_mm_min:g}|{arc_deg}"


async def _valid_tests(session: AsyncSession, machine_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT id, performed_at, template, plane, radius_mm, feed_mm_min, arc_deg FROM ballbar_tests "
            "WHERE machine_id = :m AND voided_at IS NULL ORDER BY performed_at, id"
        ),
        {"m": machine_id},
    )
    tests = [dict(r._mapping) for r in rows]
    for t in tests:
        t["key"] = group_key(t["template"], t["plane"], t["radius_mm"], t["feed_mm_min"], t["arc_deg"])
    return tests


async def groups(session: AsyncSession, machine_id: int) -> list[dict[str, Any]]:
    """Groups of comparable tests, the most recently tested first; set-up fields from the latest test."""
    by_key: dict[str, dict[str, Any]] = {}
    for t in await _valid_tests(session, machine_id):  # oldest first: the latest test overwrites the set-up
        group = by_key.setdefault(t["key"], {"key": t["key"], "tests": 0})
        group.update(
            {
                "template": t["template"] if t["key"].startswith("template:") else None,
                "plane": t["plane"],
                "radius_mm": t["radius_mm"],
                "feed_mm_min": t["feed_mm_min"],
                "arc_deg": t["arc_deg"],
                "last": t["performed_at"],
                "tests": group["tests"] + 1,
            }
        )
    return sorted(by_key.values(), key=lambda g: g["last"], reverse=True)


async def events(session: AsyncSession, machine_id: int, since: datetime) -> list[dict[str, Any]]:
    """Collision marks and completed repairs after ``since``: events shown on the trend. Voided ones are left out."""
    found = [
        {"kind": "collision", "ts": c["ts"], "text": c["comment"]}
        for c in await journal.list_collisions(session, machine_id, since)
        if c["voided_at"] is None
    ]
    found += [
        {"kind": "repair", "ts": r["completed_at"], "text": r["description"]}
        for r in await records.list_repairs(session, machine_id)
        if r["completed_at"] > since and r["voided_at"] is None
    ]
    return sorted(found, key=lambda e: e["ts"])


async def trend(
    session: AsyncSession, machine_id: int, key: str, parameter: str, axis: str | None, direction: str | None
) -> dict[str, Any]:
    """Values of one parameter over the comparable tests of a group, with events in between."""
    tests = [t for t in await _valid_tests(session, machine_id) if t["key"] == key]
    if not tests:
        raise GeometryInputError("no tests in this group")
    entry = check_result(tests[-1]["plane"], parameter, axis, direction)
    values = await _results(session, [t["id"] for t in tests])
    points = [
        {"test_id": t["id"], "performed_at": t["performed_at"], "value": r["value"]}
        for t in tests
        for r in values[t["id"]]
        if (r["parameter"], r["axis"], r["direction"]) == (parameter, axis, direction)
    ]
    return {
        "parameter": parameter,
        "unit": entry.unit,
        "points": points,
        "events": await events(session, machine_id, tests[0]["performed_at"]),
    }


# ---------------------------------------------------------------- archive index


async def overview(session: AsyncSession, machine_ids: Collection[int], now: datetime) -> dict[int, dict[str, Any]]:
    """Last valid test of each machine and how many days ago it was."""
    rows = await session.execute(
        text(
            "SELECT machine_id, max(performed_at) AS last_test_at, count(*) AS tests FROM ballbar_tests "
            "WHERE machine_id = ANY(:ids) AND voided_at IS NULL GROUP BY machine_id"
        ),
        {"ids": list(machine_ids)},
    )
    found = {r.machine_id: (r.last_test_at, int(r.tests)) for r in rows}
    result = {}
    for machine_id in machine_ids:
        last, count = found.get(machine_id, (None, 0))
        days = None if last is None else max(0, math.floor((now - last).total_seconds() / 86400))
        result[machine_id] = {"last_test_at": last, "days_since": days, "tests": count}
    return result
