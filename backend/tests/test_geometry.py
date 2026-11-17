"""Geometric accuracy on a real PostgreSQL + TimescaleDB, through the HTTP API."""

from __future__ import annotations

import contextlib
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import identity
from app.api.main import app
from app.core.config import Settings
from app.worker.__main__ import Worker
from tests.conftest import Seed, put_samples, seed_reference

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings()
NOW = datetime.now(UTC)
PASSWORD = "secret-password"
PDF = b"%PDF-1.4\n% ballbar report\n%%EOF\n"
B5R = bytes(range(256)) * 40


@dataclass
class Shop:
    m1: Seed  # site a1, model VF-500
    m2: Seed  # site a2 of the same organization, model VF-500
    tokens: dict[str, str] = field(default_factory=dict)


@pytest.fixture
async def shop(session_factory: Factory) -> Shop:
    m1 = await seed_reference(session_factory, org="geo", site="a1", machine="m1", collector="col-a1")
    m2 = await seed_reference(session_factory, org="geo", site="a2", machine="m2", collector="col-a2")
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE machines SET model = 'VF-500'"))
        await identity.bootstrap(session, SETTINGS)
        users = {
            "engineer": ("engineer", m1.org_id, m1.site_id, None),
            "master": ("master", m1.org_id, m1.site_id, m1.area_id),
            "operator": ("operator", m1.org_id, m1.site_id, m1.area_id),
            "orgadmin": ("admin", m1.org_id, None, None),
            "service": ("service", None, None, None),
        }
        tokens = {}
        for login, (role, org, site, area) in users.items():
            user_id = await identity.create_user(session, login, PASSWORD, login.title())
            await identity.add_membership(session, user_id, role, org, site, area)
            token = await identity.login(session, SETTINGS, login, PASSWORD)
            assert token is not None
            tokens[login] = token
        _, tokens["kiosk"] = await identity.issue_device_token(session, "kiosk m1", m1.machine_id, None, None)
    m1.machine["model"] = m2.machine["model"] = "VF-500"
    app.state.settings = SETTINGS
    app.state.session_factory = session_factory
    app.state.redis = None
    return Shop(m1, m2, tokens)


@contextlib.asynccontextmanager
async def client(token: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1", headers=headers) as http:
        yield http


def ballbar(
    days_ago: float, plane: str = "XY", radius: float = 100, feed: float = 1000, **values: float
) -> dict[str, Any]:
    """A test body; ``values`` like squareness=12, backlash_X=2.5, circularity_CW=9."""
    results = []
    for key, value in values.items():
        parameter, _, qualifier = key.partition("__")
        axis = qualifier if qualifier in ("X", "Y", "Z") else None
        direction = qualifier if qualifier in ("CW", "CCW") else None
        results.append({"parameter": parameter, "axis": axis, "direction": direction, "value": value})
    return {
        "performed_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "engineer": "Иванов И. И.",
        "plane": plane,
        "radius_mm": radius,
        "feed_mm_min": feed,
        "arc_deg": 360 if plane == "XY" else 220,
        "air_temp_c": 20.5,
        "machine_temp_c": 22.0,
        "template": "XY 100 mm F1000",
        "results": results,
    }


async def create(http: httpx.AsyncClient, machine_id: int, body: dict[str, Any]) -> int:
    response = await http.post(f"/machines/{machine_id}/ballbar/tests", json=body)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_ballbar_test_with_results_files_and_points(shop: Shop, session_factory: Factory) -> None:
    m1 = shop.m1.machine_id
    async with client(shop.tokens["engineer"]) as http:
        body = ballbar(1, squareness=-12.4, backlash__X=2.1, backlash__Y=-1.5, circularity=9.8, circularity__CW=8.7,
                       servo_mismatch=0.42, scaling_error__Y=18.0)  # fmt: skip
        test_id = await create(http, m1, body)
        for wrong in (
            ballbar(1, backlash__Z=1.0),  # Z is not an axis of the XY plane
            ballbar(1, squareness__X=1.0),  # squareness has no axis
            ballbar(1, flatness=1.0),
            ballbar(-3, squareness=1.0),  # in the future
        ):
            response = await http.post(f"/machines/{m1}/ballbar/tests", json=wrong)
            assert response.status_code == 422, wrong
        twice = ballbar(1)
        twice["results"] = [{"parameter": "squareness", "value": 1.0}, {"parameter": "squareness", "value": 2.0}]
        assert (await http.post(f"/machines/{m1}/ballbar/tests", json=twice)).status_code == 422

        test = (await http.get(f"/ballbar/tests/{test_id}")).json()
        assert (test["plane"], test["radius_mm"], test["feed_mm_min"], test["arc_deg"]) == ("XY", 100, 1000, 360)
        units = {(r["parameter"], r["axis"], r["direction"]): (r["value"], r["unit"]) for r in test["results"]}
        assert units[("squareness", None, None)] == (-12.4, "µm/m")
        assert units[("servo_mismatch", None, None)] == (0.42, "ms")
        assert units[("scaling_error", "Y", None)] == (18.0, "ppm")
        assert units[("circularity", None, "CW")] == (8.7, "µm")
        assert len(units) == 7

        # originals: the .b5r and the PDF report, both kept as uploaded
        pdf = await http.post(
            f"/ballbar/tests/{test_id}/files", files={"file": ("Отчёт XY.pdf", PDF, "application/pdf")}
        )
        b5r = await http.post(f"/ballbar/tests/{test_id}/files", files={"file": ("test.b5r", B5R)})
        assert (pdf.status_code, b5r.status_code) == (201, 201)
        assert (pdf.json()["kind"], b5r.json()["kind"], b5r.json()["size_bytes"]) == ("pdf", "b5r", len(B5R))
        fake = await http.post(f"/ballbar/tests/{test_id}/files", files={"file": ("report.pdf", b"not a pdf")})
        other = await http.post(f"/ballbar/tests/{test_id}/files", files={"file": ("photo.jpg", b"\xff\xd8")})
        assert (fake.status_code, other.status_code) == (422, 422)
        download = await http.get(f"/ballbar/files/{b5r.json()['id']}")
        assert download.content == B5R
        assert "filename*=UTF-8''test.b5r" in download.headers["content-disposition"]
        pdf_back = await http.get(f"/ballbar/files/{pdf.json()['id']}")
        assert pdf_back.content == PDF and pdf_back.headers["content-type"] == "application/pdf"
        assert "%D0%9E%D1%82%D1%87%D1%91%D1%82" in pdf_back.headers["content-disposition"]  # Cyrillic name kept

        # raw points of the circle for the polar plot
        angles = [i * 0.1 for i in range(3600)]
        deviations = [3 * math.sin(math.radians(2 * a)) for a in angles]
        trace = {"direction": "CW", "angle_deg": angles, "deviation_um": deviations, "sample_rate_hz": 1000}
        assert (await http.put(f"/ballbar/tests/{test_id}/traces", json=trace)).json() == {"points": 3600}
        bad = {**trace, "deviation_um": deviations[:-1]}
        assert (await http.put(f"/ballbar/tests/{test_id}/traces", json=bad)).status_code == 422
        (plot,) = (await http.get(f"/ballbar/tests/{test_id}/traces", params={"max_points": 100})).json()
        assert (plot["direction"], plot["points"], len(plot["angle_deg"])) == ("CW", 3600, 100)
        assert plot["angle_deg"][1] == pytest.approx(3.6)

        listed = (await http.get(f"/machines/{m1}/ballbar/tests")).json()
        assert [(t["id"], t["files"], t["has_traces"], t["results_count"]) for t in listed] == [(test_id, 2, True, 7)]

        # a wrong test is voided, not deleted; afterwards it is read-only
        assert (await http.post(f"/ballbar/tests/{test_id}/void", json={"reason": "не та подача"})).status_code == 200
        assert (await http.put(f"/ballbar/tests/{test_id}", json=body)).status_code == 409
        late = await http.post(f"/ballbar/tests/{test_id}/files", files={"file": ("x.pdf", PDF)})
        assert late.status_code == 409
        assert (await http.get(f"/ballbar/tests/{test_id}")).json()["void_reason"] == "не та подача"

    # the originals cannot be changed or removed even directly in the database
    for statement in ("UPDATE ballbar_files SET filename = 'x'", "DELETE FROM ballbar_files"):
        with pytest.raises(DBAPIError, match="kept forever"):
            async with session_factory() as session, session.begin():
                await session.execute(text(statement))
    async with session_factory() as session:
        assert (await session.execute(text("SELECT count(*) FROM ballbar_files"))).scalar_one() == 2


async def test_who_may_read_and_write_geometry(shop: Shop) -> None:
    m1, m2 = shop.m1.machine_id, shop.m2.machine_id
    async with client(shop.tokens["engineer"]) as http:
        test_id = await create(http, m1, ballbar(2, squareness=5))
        assert (await http.post(f"/machines/{m2}/ballbar/tests", json=ballbar(2))).status_code == 404  # other site
    async with client(shop.tokens["master"]) as http:  # reads, does not write
        assert (await http.get(f"/ballbar/tests/{test_id}")).status_code == 200
        assert (await http.post(f"/machines/{m1}/ballbar/tests", json=ballbar(2))).status_code == 403
        assert (await http.put(f"/machines/{m1}/ballbar/policy", json={"interval_days": 30})).status_code == 403
        assert (await http.post(f"/machines/{m1}/collisions", json={"comment": "удар"})).status_code == 201
    async with client(shop.tokens["operator"]) as http:  # marks a collision, sees no geometry
        assert (await http.post(f"/machines/{m1}/collisions", json={"comment": "удар"})).status_code == 201
        assert (await http.get(f"/machines/{m1}/ballbar/tests")).status_code == 403
        assert (await http.get("/service/worklist")).status_code == 403
        assert (await http.get("/auth/me")).json()["menu"]["service"] is False
    async with client(shop.tokens["kiosk"]) as http:
        assert (await http.post(f"/machines/{m1}/collisions", json={})).status_code == 201
        assert (await http.post(f"/machines/{m2}/collisions", json={})).status_code == 404
        assert (await http.get(f"/ballbar/tests/{test_id}")).status_code == 403
    async with client(shop.tokens["service"]) as http:  # the service company checks machines of every customer
        assert (await http.post(f"/machines/{m2}/ballbar/tests", json=ballbar(1, squareness=3))).status_code == 201
        files = {"file": ("r.pdf", PDF)}
        assert (await http.post(f"/ballbar/tests/{test_id}/files", files=files)).status_code == 201
        assert (await http.get("/auth/me")).json()["menu"]["service"] is True


async def test_trend_uses_only_tests_with_the_same_set_up(shop: Shop) -> None:
    m1 = shop.m1.machine_id
    async with client(shop.tokens["service"]) as http:
        limit = {"machine_model": "VF-500", "parameter": "squareness", "plane": "XY", "limit_value": 15}
        assert (await http.post("/geometry/tolerances", json=limit)).status_code == 201
        same = [await create(http, m1, ballbar(d, squareness=v)) for d, v in ((300, 6.0), (200, 11.0), (30, 17.5))]
        await create(http, m1, ballbar(250, feed=2000, squareness=30))  # other feed
        await create(http, m1, ballbar(150, radius=150, squareness=40))  # other radius
        await create(http, m1, ballbar(100, plane="ZX", squareness=50))  # other plane
        wrong = await create(http, m1, ballbar(50, squareness=99))
        await http.post(f"/ballbar/tests/{wrong}/void", json={"reason": "датчик не откалиброван"})

        params = {"parameter": "squareness", "plane": "XY", "radius_mm": 100, "feed_mm_min": 1000}
        trend = (await http.get(f"/machines/{m1}/ballbar/trend", params=params)).json()
        assert [p["test_id"] for p in trend["points"]] == same  # oldest first
        assert [p["value"] for p in trend["points"]] == [6.0, 11.0, 17.5]
        assert [p["exceeded"] for p in trend["points"]] == [False, False, True]
        assert (trend["unit"], trend["limit"], trend["limit_source"]) == ("µm/m", 15, "model")
        bad = await http.get(f"/machines/{m1}/ballbar/trend", params={**params, "axis": "X"})
        assert bad.status_code == 422  # squareness has no axis

        setups = (await http.get(f"/machines/{m1}/ballbar/status")).json()["configurations"]
        assert sorted((c["plane"], c["radius_mm"], c["feed_mm_min"], c["tests"]) for c in setups) == [
            ("XY", 100, 1000, 3), ("XY", 100, 2000, 1), ("XY", 150, 1000, 1), ("ZX", 100, 1000, 1),
        ]  # fmt: skip


async def test_machine_tolerance_overrides_the_model(shop: Shop) -> None:
    m1, m2 = shop.m1.machine_id, shop.m2.machine_id
    model = {"machine_model": "VF-500", "parameter": "backlash", "limit_value": 4}
    async with client(shop.tokens["orgadmin"]) as http:  # models are shared by all customers: not the admin's to set
        assert (await http.post("/geometry/tolerances", json=model)).status_code == 403
    async with client(shop.tokens["service"]) as http:
        assert (await http.post("/geometry/tolerances", json=model)).status_code == 201
        assert (await http.post("/geometry/tolerances", json=model)).status_code == 409
    async with client(shop.tokens["engineer"]) as http:
        own = {"machine_id": m1, "parameter": "backlash", "axis": "X", "limit_value": 6}
        assert (await http.post("/geometry/tolerances", json=own)).status_code == 201
        assert (await http.post("/geometry/tolerances", json={**own, "machine_id": m2})).status_code == 404
        assert (await http.post("/geometry/tolerances", json={**own, "parameter": "squareness"})).status_code == 422
        values = {"backlash__X": 5.0, "backlash__Y": 5.0}
        first = await create(http, m1, ballbar(1, **values))
        listed = (await http.get("/geometry/tolerances")).json()
        assert listed["models"] == ["VF-500"] and listed["models_editable"] is False
        assert {(r["machine_model"], r["machine_id"], r["editable"]) for r in listed["rows"]} == {
            ("VF-500", None, False), (None, m1, True),
        }  # fmt: skip
        results = {r["axis"]: r for r in (await http.get(f"/ballbar/tests/{first}")).json()["results"]}
    # X: the machine allows 6 µm -> within; Y: the model allows 4 µm -> exceeded
    assert (results["X"]["limit"], results["X"]["limit_source"], results["X"]["exceeded"]) == (6, "machine", False)
    assert (results["Y"]["limit"], results["Y"]["limit_source"], results["Y"]["exceeded"]) == (4, "model", True)
    async with client(shop.tokens["service"]) as http:
        other = await create(http, m2, ballbar(1, **values))
        exceeded = {r["axis"] for r in (await http.get(f"/ballbar/tests/{other}")).json()["results"] if r["exceeded"]}
    assert exceeded == {"X", "Y"}  # the other machine of the model has no own tolerance


async def test_next_check_and_service_worklist(shop: Shop, session_factory: Factory) -> None:
    m1, m2 = shop.m1, shop.m2
    tested_at = NOW - timedelta(hours=3)
    async with client(shop.tokens["service"]) as http:
        await http.post(
            "/geometry/tolerances", json={"machine_model": "VF-500", "parameter": "squareness", "limit_value": 15}
        )
        body = ballbar(0, squareness=21.0, circularity=7.0)
        body["performed_at"] = tested_at.isoformat()
        test_id = await create(http, m1.machine_id, body)
        policy = {"interval_days": 30, "interval_run_hours": 10, "on_alarm": True, "on_collision": True}
        assert (await http.put(f"/machines/{m1.machine_id}/ballbar/policy", json=policy)).status_code == 200

    # one hour of running after the test, seen by monitoring
    start = NOW - timedelta(hours=2)
    running = [{"ts": start + timedelta(seconds=s), "run": "START"} for s in range(0, 3600, 10)]
    await put_samples(session_factory, m1.machine_id, running, collector="col-a1")
    worker = Worker(SETTINGS, session_factory)
    for _ in range(3):
        await worker.rebuild_timeline(m1.machine, NOW)

    async with client(shop.tokens["service"]) as http:
        state = (await http.get(f"/machines/{m1.machine_id}/ballbar/status")).json()
        check = state["next_check"]
        assert check["status"] == "ok"
        assert check["run_hours_since"] == pytest.approx(1.0 + 30 / 3600, abs=0.01)  # the last sample holds 30 s
        assert check["run_hours_left"] == pytest.approx(9.0, abs=0.02)
        assert check["due_date"].startswith((tested_at + timedelta(days=30)).date().isoformat())
        assert [p["plane"] for p in state["planes"]] == ["XY"]
        assert [e["parameter"] for e in state["planes"][0]["exceeded"]] == ["squareness"]
        assert state["policy"]["is_default"] is False

        # a servo alarm after the test makes the check due at once
        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO machine_events "
                    "(msg_id, ts, machine_id, collector_id, seq, type, code, message, payload) "
                    "VALUES (gen_random_uuid(), :ts, :m, 'col-a1', 999, 'alarm_raised', 'SV0401', 'V-READY OFF', "
                    '\'{"category": "servo"}\')'
                ),
                {"ts": NOW - timedelta(minutes=30), "m": m1.machine_id},
            )
        await http.post(f"/machines/{m1.machine_id}/collisions", json={"comment": "столкновение шпинделя"})
        check = (await http.get(f"/machines/{m1.machine_id}/ballbar/status")).json()["next_check"]
        assert check["status"] == "event"
        assert [(t["kind"], t["code"]) for t in check["triggers"]] == [("alarm", "SV0401"), ("collision", None)]

        items = (await http.get("/service/worklist")).json()
    summary = [(i["machine_id"], i["kind"], i["severity"]) for i in items]
    assert summary == [
        (m1.machine_id, "tolerance", "high"),
        (m1.machine_id, "check_event", "high"),
        (m2.machine_id, "check_never", "low"),
    ] or summary == [
        (m1.machine_id, "check_event", "high"),
        (m1.machine_id, "tolerance", "high"),
        (m2.machine_id, "check_never", "low"),
    ]
    tolerance = next(i for i in items if i["kind"] == "tolerance")
    assert (tolerance["test_id"], tolerance["details"][0]["value"], tolerance["details"][0]["limit"]) == (
        test_id,
        21,
        15,
    )

    # a new passing test closes both items of the machine
    async with client(shop.tokens["service"]) as http:
        passing = ballbar(0, squareness=9.0)
        passing["performed_at"] = datetime.now(UTC).isoformat()  # after the collision mark
        await create(http, m1.machine_id, passing)
        items = (await http.get("/service/worklist")).json()
        assert [(i["machine_id"], i["kind"]) for i in items] == [(m2.machine_id, "check_never")]
    async with client(shop.tokens["master"]) as http:  # a master sees only the own area
        assert [i["machine_id"] for i in (await http.get("/service/worklist")).json()] == []


async def test_demo_seed_fills_the_stand_once(session_factory: Factory) -> None:
    from app.geometry import demo

    sim01 = await seed_reference(session_factory, org="demo", site="plant1", machine="sim01", collector="edge-sim-a")
    await seed_reference(session_factory, org="demo", site="plant2", machine="sim04", collector="edge-sim-b")
    for _ in range(2):  # idempotent
        async with session_factory() as session, session.begin():
            await demo.seed(session, NOW)
    async with session_factory() as session:
        counts = (await session.execute(text("SELECT count(*), count(DISTINCT machine_id) FROM ballbar_tests"))).one()
        traces = (
            await session.execute(text("SELECT direction, cardinality(angle_deg) FROM ballbar_traces ORDER BY 1"))
        ).all()
        pdf = (await session.execute(text("SELECT content FROM ballbar_files"))).scalar_one()
    assert tuple(counts) == (6, 2)  # sim01: four XY tests and one ZX; sim04: one
    assert [tuple(t) for t in traces] == [("CCW", 1440), ("CW", 1440)]
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")

    from app.admin import service as admin
    from app.geometry import service as geometry

    async with session_factory() as session:
        machines = await admin.list_machines(session, [sim01.machine_id])
        items = await geometry.worklist(session, machines, NOW)
    tolerance = next(i for i in items if i["kind"] == "tolerance")
    assert {d["parameter"] for d in tolerance["details"]} == {"squareness"}  # 17.5 µm/m against 15
