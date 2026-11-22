"""The geometry archive of the service on a real PostgreSQL + TimescaleDB, through the HTTP API."""

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
from tests.conftest import Seed, seed_reference

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings()
NOW = datetime.now(UTC)
PASSWORD = "secret-password"
PDF = b"%PDF-1.4\n% ballbar report\n%%EOF\n"
B5R = bytes(range(256)) * 40
CUSTOMER = ("engineer", "master", "operator", "orgadmin", "kiosk")


@dataclass
class Shop:
    m1: Seed  # site a1
    m2: Seed  # site a2 of the same organization
    tokens: dict[str, str] = field(default_factory=dict)


@pytest.fixture
async def shop(session_factory: Factory) -> Shop:
    m1 = await seed_reference(session_factory, org="geo", site="a1", machine="m1", collector="col-a1")
    m2 = await seed_reference(session_factory, org="geo", site="a2", machine="m2", collector="col-a2")
    async with session_factory() as session, session.begin():
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
        superadmin = await identity.login(session, SETTINGS, SETTINGS.admin_username, SETTINGS.admin_password)
        assert superadmin is not None
        tokens["superadmin"] = superadmin
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
    days_ago: float,
    plane: str = "XY",
    radius: float = 100,
    feed: float = 1000,
    arc: int | None = None,
    template: str | None = None,
    **values: float,
) -> dict[str, Any]:
    """A test body; ``values`` like squareness=12, backlash__X=2.5, circularity__CW=9."""
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
        "arc_deg": arc or (360 if plane == "XY" else 220),
        "air_temp_c": 20.5,
        "machine_temp_c": 22.0,
        "template": template,
        "results": results,
    }


async def create(http: httpx.AsyncClient, machine_id: int, body: dict[str, Any]) -> int:
    response = await http.post(f"/machines/{machine_id}/ballbar/tests", json=body)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def test_ballbar_test_with_results_files_and_points(shop: Shop, session_factory: Factory) -> None:
    m1 = shop.m1.machine_id
    async with client(shop.tokens["service"]) as http:
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
        assert not {"limit", "exceeded"} & set(test["results"][0])  # an archive: no tolerances, no verdicts

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


async def test_the_archive_exists_only_for_the_service(shop: Shop) -> None:
    """Customers get 404 on every geometry route, even for their own machines; they may only mark a collision."""
    m1 = shop.m1.machine_id
    async with client(shop.tokens["service"]) as http:
        test_id = await create(http, m1, ballbar(2, squareness=5))
        file_id = (await http.post(f"/ballbar/tests/{test_id}/files", files={"file": ("r.pdf", PDF)})).json()["id"]
        group = (await http.get(f"/machines/{m1}/ballbar/groups")).json()[0]["key"]
        assert (await http.get("/auth/me")).json()["menu"]["service"] is True

    reads = [
        "/geometry/catalog",
        "/geometry/machines",
        f"/machines/{m1}/ballbar/tests",
        f"/ballbar/tests/{test_id}",
        f"/ballbar/tests/{test_id}/traces",
        f"/ballbar/files/{file_id}",
        f"/machines/{m1}/ballbar/groups",
        f"/machines/{m1}/ballbar/trend?group={group}&parameter=squareness",
        f"/machines/{m1}/collisions",
        f"/machines/{m1}/repairs",
    ]
    for who in CUSTOMER:
        async with client(shop.tokens[who]) as http:
            for path in reads:
                assert (await http.get(path)).status_code == 404, (who, path)
            assert (await http.post(f"/machines/{m1}/ballbar/tests", json=ballbar(1))).status_code == 404, who
            assert (
                await http.post(f"/ballbar/tests/{test_id}/files", files={"file": ("x.pdf", PDF)})
            ).status_code == 404
            repair = {"completed_at": NOW.isoformat(), "description": "замена ШВП"}
            assert (await http.post(f"/machines/{m1}/repairs", json=repair)).status_code == 404, who
            assert (await http.get("/auth/me")).json()["menu"]["service"] is False, who

    # the collision mark is an event entered on the shop floor
    for who in ("operator", "master", "kiosk"):
        async with client(shop.tokens[who]) as http:
            assert (await http.post(f"/machines/{m1}/collisions", json={"comment": who})).status_code == 201, who
    async with client(shop.tokens["engineer"]) as http:  # an engineer is not on the shop floor
        assert (await http.post(f"/machines/{m1}/collisions", json={})).status_code == 403
    async with client(shop.tokens["kiosk"]) as http:
        assert (await http.post(f"/machines/{shop.m2.machine_id}/collisions", json={})).status_code == 404
    for who in ("service", "superadmin"):  # only the service sees the marks
        async with client(shop.tokens[who]) as http:
            marks = (await http.get(f"/machines/{m1}/collisions")).json()
            assert sorted(m["comment"] for m in marks) == ["kiosk", "master", "operator"], who


async def test_trend_groups_tests_by_template_or_full_set_up(shop: Shop) -> None:
    m1 = shop.m1.machine_id
    async with client(shop.tokens["service"]) as http:
        # tests with the same template are comparable even if the set-up was typed differently
        templated = [
            await create(http, m1, ballbar(300, template="XY-100", squareness=6.0)),
            await create(http, m1, ballbar(200, template=" xy-100 ", radius=100.0, squareness=11.0)),
            await create(http, m1, ballbar(30, template="XY-100", feed=1200, squareness=17.5)),
        ]
        # without a template: plane, radius, feed and arc must all match
        plain = [await create(http, m1, ballbar(d, squareness=v)) for d, v in ((250, 4.0), (120, 5.0))]
        await create(http, m1, ballbar(110, arc=220, squareness=30))  # other arc
        await create(http, m1, ballbar(100, feed=2000, squareness=40))  # other feed
        wrong = await create(http, m1, ballbar(50, squareness=99))
        await http.post(f"/ballbar/tests/{wrong}/void", json={"reason": "датчик не откалиброван"})

        # events between the tests: a collision mark and a completed repair; before the first test: ignored
        await http.post(
            f"/machines/{m1}/collisions", json={"ts": (NOW - timedelta(days=260)).isoformat(), "comment": "удар"}
        )
        await http.post(
            f"/machines/{m1}/collisions", json={"ts": (NOW - timedelta(days=400)).isoformat(), "comment": "давно"}
        )
        repair = {"completed_at": (NOW - timedelta(days=150)).isoformat(), "description": "замена ШВП оси X"}
        assert (await http.post(f"/machines/{m1}/repairs", json=repair)).status_code == 201

        groups = {g["key"]: g for g in (await http.get(f"/machines/{m1}/ballbar/groups")).json()}
        assert {k: g["tests"] for k, g in groups.items()} == {
            "template:xy-100": 3,
            "setup:XY|100|1000|360": 2,  # the voided test is left out
            "setup:XY|100|1000|220": 1,
            "setup:XY|100|2000|360": 1,
        }
        assert groups["template:xy-100"]["template"] == "XY-100" and groups["template:xy-100"]["feed_mm_min"] == 1200

        def trend(key: str, **extra: str) -> Any:
            return http.get(f"/machines/{m1}/ballbar/trend", params={"group": key, "parameter": "squareness", **extra})

        by_template = (await trend("template:xy-100")).json()
        assert [p["test_id"] for p in by_template["points"]] == templated  # oldest first
        assert [p["value"] for p in by_template["points"]] == [6.0, 11.0, 17.5]
        assert by_template["unit"] == "µm/m"
        assert [(e["kind"], e["text"]) for e in by_template["events"]] == [
            ("collision", "удар"),
            ("repair", "замена ШВП оси X"),
        ]
        by_setup = (await trend("setup:XY|100|1000|360")).json()
        assert [p["test_id"] for p in by_setup["points"]] == plain
        assert (await trend("setup:XY|100|1000|360", axis="X")).status_code == 422  # squareness has no axis
        assert (await trend("setup:ZX|1|1|360")).status_code == 422  # no such group


async def test_voided_collisions_and_repairs_leave_the_trend(shop: Shop) -> None:
    """Marks and repairs are voided with a reason like tests: kept in the lists, gone from the trend."""
    m1 = shop.m1.machine_id
    async with client(shop.tokens["operator"]) as http:
        mark = {"ts": (NOW - timedelta(days=50)).isoformat(), "comment": "задел тиски"}
        wrong_mark = (await http.post(f"/machines/{m1}/collisions", json=mark)).json()["id"]
    async with client(shop.tokens["service"]) as http:
        for days in (100, 10):
            await create(http, m1, ballbar(days, squareness=days / 10))
        kept_mark = (
            await http.post(f"/machines/{m1}/collisions", json={"ts": (NOW - timedelta(days=40)).isoformat()})
        ).json()["id"]
        repairs = {}
        for days, text_ in ((60, "замена ШВП оси X"), (30, "не тот станок")):
            body = {"completed_at": (NOW - timedelta(days=days)).isoformat(), "description": text_}
            repairs[text_] = (await http.post(f"/machines/{m1}/repairs", json=body)).json()["id"]
        wrong_repair = repairs["не тот станок"]

    # customers cannot void: the archive does not exist for them
    for who in CUSTOMER:
        async with client(shop.tokens[who]) as http:
            assert (await http.post(f"/collisions/{wrong_mark}/void", json={"reason": "ошибка"})).status_code == 404
            assert (await http.post(f"/repairs/{wrong_repair}/void", json={"reason": "ошибка"})).status_code == 404

    async with client(shop.tokens["service"]) as http:
        assert (await http.post(f"/collisions/{wrong_mark}/void", json={"reason": "x"})).status_code == 422
        assert (await http.post("/collisions/999999/void", json={"reason": "ошибка"})).status_code == 404
        assert (await http.post("/repairs/999999/void", json={"reason": "ошибка"})).status_code == 404
        voided = await http.post(f"/collisions/{wrong_mark}/void", json={"reason": "  столкновения не было  "})
        assert voided.status_code == 200
        assert (
            await http.post(f"/repairs/{wrong_repair}/void", json={"reason": "ремонт другого станка"})
        ).status_code == 200
        assert (await http.post(f"/collisions/{wrong_mark}/void", json={"reason": "ещё раз"})).status_code == 409
        assert (await http.post(f"/repairs/{wrong_repair}/void", json={"reason": "ещё раз"})).status_code == 409

        marks = {m["id"]: m for m in (await http.get(f"/machines/{m1}/collisions")).json()}
        assert (marks[wrong_mark]["void_reason"], marks[kept_mark]["void_reason"]) == ("столкновения не было", None)
        assert marks[wrong_mark]["voided_at"] is not None and marks[kept_mark]["voided_at"] is None
        logged = {r["id"]: r for r in (await http.get(f"/machines/{m1}/repairs")).json()}
        assert logged[wrong_repair]["void_reason"] == "ремонт другого станка"
        assert logged[repairs["замена ШВП оси X"]]["voided_at"] is None

        group = (await http.get(f"/machines/{m1}/ballbar/groups")).json()[0]["key"]
        events = (
            await http.get(f"/machines/{m1}/ballbar/trend", params={"group": group, "parameter": "squareness"})
        ).json()["events"]
    assert [(e["kind"], e["text"]) for e in events] == [("repair", "замена ШВП оси X"), ("collision", None)]


async def test_archive_index_shows_the_last_test_and_days_since(shop: Shop) -> None:
    async with client(shop.tokens["service"]) as http:
        await create(http, shop.m1.machine_id, ballbar(40, squareness=1))
        await create(http, shop.m1.machine_id, ballbar(12.5, squareness=2))
        voided = await create(http, shop.m1.machine_id, ballbar(1, squareness=3))
        await http.post(f"/ballbar/tests/{voided}/void", json={"reason": "ошибка ввода"})
        rows = {r["machine_id"]: r for r in (await http.get("/geometry/machines")).json()}
    assert (rows[shop.m1.machine_id]["days_since"], rows[shop.m1.machine_id]["tests"]) == (12, 2)
    assert rows[shop.m1.machine_id]["last_test_at"].startswith((NOW - timedelta(days=12.5)).date().isoformat())
    assert (rows[shop.m2.machine_id]["last_test_at"], rows[shop.m2.machine_id]["days_since"]) == (None, None)


async def test_demo_seed_fills_the_stand_once(session_factory: Factory) -> None:
    from app.geometry import demo

    await seed_reference(session_factory, org="demo", site="plant1", machine="sim01", collector="edge-sim-a")
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
        events = (
            await session.execute(
                text("SELECT (SELECT count(*) FROM collision_marks), (SELECT count(*) FROM repair_records)")
            )
        ).one()
    assert tuple(counts) == (6, 2)  # sim01: four XY tests and one ZX; sim04: one
    assert [tuple(t) for t in traces] == [("CCW", 1440), ("CW", 1440)]
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert tuple(events) == (1, 1)
