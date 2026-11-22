"""Isolation between organizations and between nodes of one organization (NOW.md 2.7)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import identity, journal
from app.admin import service as admin
from app.api.main import API_ROUTERS, app, ws_live
from app.core.config import Settings
from app.core.security import hash_token
from app.geometry import service as geometry
from app.worker.__main__ import Worker
from app.worker.live import live_channel, live_key
from tests.conftest import Seed, put_samples, seed_reference

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings()
NOW = datetime.now(UTC)
T0 = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
FROM = (T0 - timedelta(hours=1)).isoformat()
PASSWORD = "secret-password"
MARKER = "acme"  # every name that belongs to organization A contains it


@dataclass
class World:
    a1: Seed  # org A, site a1, first area, machine m1
    a1_second: Seed  # org A, site a1, second area, machine m2
    a2: Seed  # org A, site a2, machine m3
    b: Seed  # org B
    tokens: dict[str, str] = field(default_factory=dict)
    ids: dict[str, int] = field(default_factory=dict)


async def _user(
    session: AsyncSession, login: str, role: str | None, org: int | None, site: int | None, area: int | None
) -> int:
    user_id = await identity.create_user(session, login, PASSWORD, login)
    if role is not None:
        await identity.add_membership(session, user_id, role, org, site, area)
    return user_id


@pytest.fixture
async def world(session_factory: Factory) -> World:
    a1 = await seed_reference(session_factory, org="acme", site="a1", machine="m1", collector="col-a1")
    a2 = await seed_reference(session_factory, org="acme", site="a2", machine="m3", collector="col-a2")
    b = await seed_reference(session_factory, org="bravo", site="b1", machine="m1", collector="col-b1")
    async with session_factory() as session, session.begin():
        area2 = (
            await session.execute(
                text("INSERT INTO areas (site_id, name) VALUES (:s, 'acme a1 second area') RETURNING id"),
                {"s": a1.site_id},
            )
        ).scalar_one()
        m2 = (
            await session.execute(
                text(
                    "INSERT INTO machines (site_id, area_id, code, name, collector_id) "
                    "VALUES (:s, :a, 'm2', 'acme m2', 'col-a1') RETURNING id"
                ),
                {"s": a1.site_id, "a": area2},
            )
        ).scalar_one()
    async with session_factory() as session:
        (m2_row,) = await admin.list_machines(session, [m2])
    a1_second = Seed(m2_row, a1.org_id, a1.site_id, area2, a1.group_id, a1.planned_reason_id, a1.unplanned_reason_id)
    world = World(a1, a1_second, a2, b)

    # raw and derived data for every machine
    worker = Worker(SETTINGS, session_factory)
    for seed in (a1, a1_second, a2, b):
        collector = seed.machine["collector_id"]
        samples = [
            {"ts": T0 + timedelta(seconds=t), "run": "START" if t < 900 else "STOP", "part_count": t // 60}
            for t in range(0, 1800, 10)
        ]
        await put_samples(session_factory, seed.machine_id, samples, collector=collector)
        for _ in range(5):
            await worker.rebuild_timeline(seed.machine, NOW)
            await worker.update_oee(seed.machine, NOW)

    async with session_factory() as session, session.begin():
        await identity.bootstrap(session, SETTINGS)
        for seed, key in ((a1, "a"), (b, "b")):
            world.ids[f"annotation_{key}"] = await journal.add_annotation(
                session, seed.machine_id, T0 + timedelta(seconds=900), None, seed.unplanned_reason_id, None, None
            )
            await journal.add_scrap(session, seed.machine_id, T0 + timedelta(seconds=600), 2, None, "O1", None)
        users = {
            "admin_a": ("acme-admin", "admin", a1.org_id, None, None),
            "admin_b": ("bravo-admin", "admin", b.org_id, None, None),
            "engineer_a1": ("acme-engineer", "engineer", a1.org_id, a1.site_id, None),
            "master_a1": ("acme-master", "master", a1.org_id, a1.site_id, a1.area_id),
            "operator_a1": ("acme-operator", "operator", a1.org_id, a1.site_id, a1.area_id),
            "service": ("service-engineer", "service", None, None, None),
        }
        for key, (login, role, org, site, area) in users.items():
            world.ids[f"user_{key}"] = await _user(session, login, role, org, site, area)
        world.ids["membership_a"] = (
            await session.execute(
                text("SELECT id FROM memberships WHERE user_id = :u"), {"u": world.ids["user_master_a1"]}
            )
        ).scalar_one()
        for key, (login, *_rest) in users.items():
            token = await identity.login(session, SETTINGS, login, PASSWORD)
            assert token is not None
            world.tokens[key] = token
        superadmin = await identity.login(session, SETTINGS, SETTINGS.admin_username, SETTINGS.admin_password)
        assert superadmin is not None
        world.tokens["superadmin"] = superadmin
        world.ids["token_kiosk_a"], world.tokens["kiosk_a"] = await identity.issue_device_token(
            session, "acme kiosk m1", a1.machine_id, None, None
        )
        world.ids["token_display_a"], world.tokens["display_a"] = await identity.issue_device_token(
            session, "acme display", None, a1.area_id, None
        )
        _, world.tokens["kiosk_b"] = await identity.issue_device_token(session, "bravo kiosk", b.machine_id, None, None)

        async def first(sql: str, **params: Any) -> int:
            return int((await session.execute(text(sql), params)).scalar_one())

        org = a1.org_id
        for name, table in [
            ("shift", "shift_templates"),
            ("scrap_reason", "scrap_reasons"),
            ("program", "programs"),
            ("rules", "state_rules"),
        ]:
            world.ids[f"{name}_a"] = await first(f"SELECT min(id) FROM {table} WHERE org_id = :o", o=org)

        # geometry archive of organization A's machine: a ballbar test with a file and points, a collision, a repair
        test_in = geometry.TestIn(
            T0, "acme engineer", "XY", 100, 1000, 360, results=[geometry.ResultIn("squareness", 12.0)]
        )
        world.ids["ballbar_a"] = await geometry.create_test(session, a1.machine_id, test_in, None)
        stored = await geometry.add_file(
            session, world.ids["ballbar_a"], "acme.pdf", b"%PDF-1.4 acme report", None, "acme"
        )
        world.ids["ballbar_file_a"] = stored["id"]
        await geometry.put_trace(session, world.ids["ballbar_a"], "CW", [0.0, 120.0, 240.0], [1.0, -1.0, 0.5], None)
        await journal.add_collision(session, a1.machine_id, T0 + timedelta(minutes=5), "acme collision", None)
        await geometry.add_repair(session, a1.machine_id, T0 + timedelta(minutes=10), "acme repair", "acme", None)
    app.state.settings = SETTINGS
    app.state.session_factory = session_factory
    app.state.redis = None
    return world


@contextlib.asynccontextmanager
async def client(token: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1", headers=headers) as http:
        yield http


async def get_ids(http: httpx.AsyncClient, path: str = "/machines") -> set[int]:
    response = await http.get(path)
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


# ---------------------------------------------------------------- another organization, every route


@dataclass
class Call:
    path: str
    expect: str  # hidden: 403/404 | empty: 200 without rows | clean: 200 without anything of organization A
    json: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    files: dict[str, Any] | None = None
    archive: bool = False  # a route of the geometry archive: it exists only for the service


def foreign_calls(w: World) -> dict[tuple[str, str], list[Call]]:
    """Requests of an administrator of organization B that aim at objects of organization A."""
    a, m = w.a1, w.a1.machine_id
    period = {"from": FROM}
    aimed = {"machine_id": m, **period}
    node = {"org_id": a.org_id}
    crud_rows = {
        "organizations": a.org_id,
        "sites": a.site_id,
        "areas": a.area_id,
        "collectors": "col-a1",
        "machines": m,
        "shift-templates": w.ids["shift_a"],
        "downtime-reasons": a.planned_reason_id,
        "scrap-reasons": w.ids["scrap_reason_a"],
        "programs": w.ids["program_a"],
        "state-rules": w.ids["rules_a"],
    }
    new_rows = {
        "organizations": {"code": "x", "name": "x"},
        "sites": {**node, "code": "x", "name": "x"},
        "areas": {"site_id": a.site_id, "name": "x"},
        "collectors": {"id": "x", "site_id": a.site_id, "name": "x"},
        "shift-templates": {**node, "name": "x", "start_time": "08:00", "end_time": "16:00"},
        "downtime-reasons": {**node, "code": "X", "name": "x"},
        "scrap-reasons": {**node, "code": "X", "name": "x"},
        "programs": {**node, "program": "O1", "ideal_cycle_time_s": 1},
        "state-rules": {**node, "name": "x", "rules": {}},
    }
    membership = {"role": "operator", "org_id": a.org_id}
    test = w.ids["ballbar_a"]
    ballbar = {
        "performed_at": FROM,
        "engineer": "x",
        "plane": "XY",
        "radius_mm": 100,
        "feed_mm_min": 1000,
        "arc_deg": 360,
    }
    trace = {"direction": "CCW", "angle_deg": [0, 1, 2], "deviation_um": [0, 0, 0]}
    trend = {"group": "setup:XY|100|1000|360", "parameter": "squareness"}
    repair = {"completed_at": FROM, "description": "xyz"}

    def archive(path: str, **kwargs: Any) -> Call:
        return Call(path, "hidden", archive=True, **kwargs)

    return {
        ("GET", "/auth/me"): [Call("/auth/me", "clean")],
        ("GET", "/machines"): [Call("/machines", "clean"), Call("/machines", "empty", params={"site_id": a.site_id})],
        ("GET", "/machines/{machine_id}"): [Call(f"/machines/{m}", "hidden")],
        ("POST", "/machines/{machine_id}/setup"): [Call(f"/machines/{m}/setup", "hidden", json={"active": True})],
        ("GET", "/reasons"): [
            Call("/reasons", "hidden", params={"machine_id": m}),
            Call("/reasons", "hidden", params={"site_id": a.site_id}),
        ],
        ("GET", "/scrap-reasons"): [Call("/scrap-reasons", "hidden", params={"site_id": a.site_id})],
        ("GET", "/shifts"): [Call("/shifts", "hidden", params={"site_id": a.site_id, **period})],
        ("GET", "/collectors"): [Call("/collectors", "clean")],
        ("GET", "/timeline"): [Call("/timeline", "empty", params=aimed), Call("/timeline", "clean", params=period)],
        ("GET", "/oee"): [
            Call("/oee", "empty", params={**aimed, "period": "hour"}),
            Call("/oee", "empty", params={"site_id": a.site_id, "period": "hour", **period}),
        ],
        ("GET", "/reports/pareto"): [Call("/reports/pareto", "empty", params=aimed)],
        ("GET", "/annotations"): [
            Call("/annotations", "empty", params=aimed),
            Call("/annotations", "clean", params=period),
        ],
        ("GET", "/scrap"): [Call("/scrap", "empty", params=aimed)],
        ("POST", "/annotations"): [
            Call(
                "/annotations", "hidden", json={"machine_id": m, "start_ts": FROM, "reason_id": w.b.unplanned_reason_id}
            )
        ],
        ("DELETE", "/annotations/{annotation_id}"): [Call(f"/annotations/{w.ids['annotation_a']}", "hidden")],
        ("POST", "/scrap"): [Call("/scrap", "hidden", json={"machine_id": m, "qty": 1})],
        ("GET", "/admin/{name}"): [
            *[Call(f"/admin/{name}", "clean") for name in crud_rows],
            *[Call(f"/admin/{name}", "empty", params={"org_id": a.org_id}) for name in crud_rows],
        ],
        ("POST", "/admin/{name}"): [Call(f"/admin/{name}", "hidden", json=body) for name, body in new_rows.items()],
        ("PUT", "/admin/{name}/{row_id}"): [
            Call(f"/admin/{name}/{row}", "hidden", json={"name": "hacked"}) for name, row in crud_rows.items()
        ],
        ("DELETE", "/admin/{name}/{row_id}"): [
            Call(f"/admin/{name}/{row}", "hidden") for name, row in crud_rows.items()
        ],
        ("POST", "/recalc"): [Call("/recalc", "empty", params={"site_id": a.site_id})],
        ("GET", "/users"): [Call("/users", "clean")],
        ("POST", "/users"): [
            Call("/users", "hidden", json={"login": "intruder", "password": PASSWORD, "membership": membership})
        ],
        ("PUT", "/users/{user_id}"): [
            Call(f"/users/{w.ids['user_admin_a']}", "hidden", json={"password": "hacked-password"})
        ],
        ("POST", "/users/{user_id}/memberships"): [
            Call(f"/users/{w.ids['user_admin_a']}/memberships", "hidden", json={"role": "admin", "org_id": w.b.org_id}),
            Call(f"/users/{w.ids['user_admin_b']}/memberships", "hidden", json=membership),
        ],
        ("DELETE", "/memberships/{membership_id}"): [Call(f"/memberships/{w.ids['membership_a']}", "hidden")],
        ("GET", "/device-tokens"): [Call("/device-tokens", "clean")],
        ("POST", "/device-tokens"): [
            Call("/device-tokens", "hidden", json={"name": "x", "machine_id": m}),
            Call("/device-tokens", "hidden", json={"name": "x", "area_id": a.area_id}),
        ],
        ("DELETE", "/device-tokens/{token_id}"): [Call(f"/device-tokens/{w.ids['token_kiosk_a']}", "hidden")],
        # geometric accuracy
        # geometry archive of the service
        ("GET", "/geometry/catalog"): [archive("/geometry/catalog")],
        ("GET", "/geometry/machines"): [archive("/geometry/machines")],
        ("GET", "/machines/{machine_id}/ballbar/tests"): [archive(f"/machines/{m}/ballbar/tests")],
        ("POST", "/machines/{machine_id}/ballbar/tests"): [archive(f"/machines/{m}/ballbar/tests", json=ballbar)],
        ("GET", "/ballbar/tests/{test_id}"): [archive(f"/ballbar/tests/{test}")],
        ("PUT", "/ballbar/tests/{test_id}"): [archive(f"/ballbar/tests/{test}", json=ballbar)],
        ("POST", "/ballbar/tests/{test_id}/void"): [archive(f"/ballbar/tests/{test}/void", json={"reason": "xyz"})],
        ("POST", "/ballbar/tests/{test_id}/files"): [
            archive(f"/ballbar/tests/{test}/files", files={"file": ("x.pdf", b"%PDF-1.4 x")})
        ],
        ("GET", "/ballbar/files/{file_id}"): [archive(f"/ballbar/files/{w.ids['ballbar_file_a']}")],
        ("PUT", "/ballbar/tests/{test_id}/traces"): [archive(f"/ballbar/tests/{test}/traces", json=trace)],
        ("GET", "/ballbar/tests/{test_id}/traces"): [archive(f"/ballbar/tests/{test}/traces")],
        ("GET", "/machines/{machine_id}/ballbar/groups"): [archive(f"/machines/{m}/ballbar/groups")],
        ("GET", "/machines/{machine_id}/ballbar/trend"): [archive(f"/machines/{m}/ballbar/trend", params=trend)],
        ("POST", "/machines/{machine_id}/collisions"): [Call(f"/machines/{m}/collisions", "hidden", json={})],
        ("GET", "/machines/{machine_id}/collisions"): [archive(f"/machines/{m}/collisions")],
        ("GET", "/machines/{machine_id}/repairs"): [archive(f"/machines/{m}/repairs")],
        ("POST", "/machines/{machine_id}/repairs"): [archive(f"/machines/{m}/repairs", json=repair)],
    }


PUBLIC_ROUTES = {("GET", "/health"), ("POST", "/auth/login")}


def _is_empty(body: Any) -> bool:
    if isinstance(body, list):
        return not body
    if isinstance(body, dict):
        return all(not v for k, v in body.items() if isinstance(v, list | int) and k != "from")
    return False


async def _fingerprint(factory: Factory) -> dict[str, Any]:
    """Everything a request could have changed."""
    tables = [
        "organizations", "sites", "areas", "collectors", "machines", "shift_templates", "downtime_reasons",
        "scrap_reasons", "programs", "state_rules", "users", "memberships", "device_tokens", "downtime_annotations",
        "scrap_records", "operator_setup_marks", "timeline_dirty", "oee_dirty", "ballbar_tests", "ballbar_results",
        "ballbar_traces", "ballbar_files", "collision_marks", "repair_records",
    ]  # fmt: skip
    async with factory() as session:
        result = {}
        for table in tables:
            rows = await session.execute(
                text(f"SELECT md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), '')) FROM {table} t")
            )
            result[table] = rows.scalar_one()
        return result


async def test_another_organization_sees_nothing_on_any_route(world: World, session_factory: Factory) -> None:
    calls = foreign_calls(world)
    api_routes = {
        (method, route.path.removeprefix("/api/v1"))
        for api_router in API_ROUTERS
        for route in api_router.routes
        for method in route.methods - {"HEAD", "OPTIONS"}  # type: ignore[attr-defined]
    }
    assert len(api_routes) > 30
    assert api_routes - PUBLIC_ROUTES == set(calls), "every route must be listed in foreign_calls()"

    before = await _fingerprint(session_factory)
    async with client(world.tokens["admin_b"]) as http:
        for (method, _template), requests in calls.items():
            for call in requests:
                response = await http.request(method, call.path, json=call.json, params=call.params, files=call.files)
                sent = call.params or call.json or ""
                where = f"{method} {call.path} {sent} -> {response.status_code} {response.text[:200]}"
                assert MARKER not in response.text, where
                if call.expect == "hidden":
                    assert response.status_code in (403, 404, 405), where
                else:
                    assert response.status_code == 200, where
                    if call.expect == "empty":
                        assert _is_empty(response.json()), where
    assert await _fingerprint(session_factory) == before, "requests aimed at another organization changed data"

    # the same reading requests return the data of organization A to those who may see it:
    # its administrator, and for the geometry archive the service
    for owner in ("admin_a", "service"):
        async with client(world.tokens[owner]) as http:
            for (method, _template), requests in calls.items():
                for call in requests:
                    if method != "GET" or call.archive != (owner == "service"):
                        continue
                    response = await http.request(method, call.path, params=call.params)
                    assert response.status_code == 200, f"{owner} {call.path}: {response.text[:200]}"
                    assert MARKER in response.text or not _is_empty(response.json()), f"{call.path} {call.params}"


async def test_customers_do_not_see_the_geometry_archive_of_their_own_machines(
    world: World, session_factory: Factory
) -> None:
    """Tests, files, points, trends, collision marks and repairs exist only for the service: everyone of the
    customer gets 404 on every archive route for the machines of their own organization."""
    calls = [
        (method, call) for (method, _t), requests in foreign_calls(world).items() for call in requests if call.archive
    ]
    assert len(calls) == 16  # every geometry route except marking a collision
    before = await _fingerprint(session_factory)
    for who in ("admin_a", "engineer_a1", "master_a1", "operator_a1", "kiosk_a", "display_a"):
        async with client(world.tokens[who]) as http:
            assert (await http.get("/auth/me")).json()["menu"]["service"] is False, who
            for method, call in calls:
                response = await http.request(method, call.path, json=call.json, params=call.params, files=call.files)
                assert response.status_code == 404, f"{who} {method} {call.path} -> {response.status_code}"
                assert MARKER not in response.text, f"{who} {method} {call.path}"
    assert await _fingerprint(session_factory) == before


async def test_unauthenticated_requests_are_rejected(world: World) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
        for (method, template), requests in foreign_calls(world).items():
            for token in (None, "garbage", "dev_garbage"):
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                first_call = requests[0]
                response = await http.request(
                    method, first_call.path, headers=headers, json=first_call.json, files=first_call.files
                )
                assert response.status_code == 401, f"{method} {template}"


# ---------------------------------------------------------------- nodes inside one organization


async def test_master_sees_own_area_engineer_sees_whole_site(world: World) -> None:
    m1, m2, m3 = world.a1.machine_id, world.a1_second.machine_id, world.a2.machine_id
    period = {"from": FROM, "period": "hour"}

    async with client(world.tokens["master_a1"]) as http:
        assert await get_ids(http) == {m1}
        assert (await http.get(f"/machines/{m2}")).status_code == 404  # the neighbouring area does not exist for them
        rows = (await http.get("/oee", params={**period, "machine_id": [m1, m2, m3]})).json()
        assert {r["machine_id"] for r in rows} == {m1}
        intervals = (await http.get("/timeline", params={"from": FROM})).json()["intervals"]
        assert {i["machine_id"] for i in intervals} == {m1}
        # a master corrects downtime reasons of the area, but does not edit norms or structure
        assert (await http.delete(f"/annotations/{world.ids['annotation_a']}")).status_code == 204
        program = {"org_id": world.a1.org_id, "site_id": world.a1.site_id, "program": "O7", "ideal_cycle_time_s": 10}
        assert (await http.post("/admin/programs", json=program)).status_code == 403
        assert (await http.get("/users")).status_code == 403

    async with client(world.tokens["engineer_a1"]) as http:
        assert await get_ids(http) == {m1, m2}  # the whole site, not the other site of the organization
        assert (await http.get(f"/machines/{m3}")).status_code == 404
        rows = (await http.get("/oee", params=period)).json()
        assert {r["machine_id"] for r in rows} == {m1, m2}
        # norms of their own site only
        program = {"org_id": world.a1.org_id, "site_id": world.a1.site_id, "program": "O7", "ideal_cycle_time_s": 10}
        assert (await http.post("/admin/programs", json=program)).status_code == 201
        assert (await http.post("/admin/programs", json={**program, "site_id": world.a2.site_id})).status_code == 403
        assert (await http.post("/admin/programs", json={**program, "site_id": None})).status_code == 403
        shift = {"org_id": world.a1.org_id, "name": "x", "start_time": "08:00", "end_time": "16:00"}
        assert (await http.post("/admin/shift-templates", json=shift)).status_code == 403  # shifts are the admin's
        # an engineer reads shifts and reasons, but has no operator input
        assert (await http.get("/admin/shift-templates")).json() != []
        scrap = {"machine_id": m1, "qty": 1}
        assert (await http.post("/scrap", json=scrap)).status_code == 403

    async with client(world.tokens["operator_a1"]) as http:
        assert await get_ids(http) == {m1}
        assert (await http.post("/scrap", json={"machine_id": m1, "qty": 1})).status_code == 201
        assert (await http.get("/timeline", params={"from": FROM})).status_code == 403  # no analytics for operators
        assert (await http.get("/admin/programs")).json() == []

    async with client(world.tokens["admin_a"]) as http:
        assert await get_ids(http) == {m1, m2, m3}

    async with client(world.tokens["service"]) as http:  # service company: machines of every customer, read only
        assert await get_ids(http) == {m1, m2, m3, world.b.machine_id}
        assert len((await http.get("/timeline", params={"from": FROM})).json()["intervals"]) >= 4
        assert {c["id"] for c in (await http.get("/collectors")).json()} == {"col-a1", "col-a2", "col-b1"}
        assert (await http.post("/scrap", json={"machine_id": m1, "qty": 1})).status_code == 403
        assert (await http.get("/admin/programs")).json() == []
        assert (await http.get("/users")).status_code == 403


async def test_kiosk_token_is_bound_to_one_machine_or_area(world: World, session_factory: Factory) -> None:
    m1, m2 = world.a1.machine_id, world.a1_second.machine_id
    reason = world.a1.unplanned_reason_id

    async with client(world.tokens["kiosk_a"]) as http:  # operator screen of machine m1
        assert await get_ids(http) == {m1}
        assert (await http.get("/auth/me")).json()["kiosk"] == {"machine_id": m1, "area_id": None}
        assert (await http.get(f"/machines/{m2}")).status_code == 404
        assert (await http.get(f"/machines/{world.b.machine_id}")).status_code == 404
        annotation = {"machine_id": m1, "start_ts": FROM, "reason_id": reason}
        assert (await http.post("/annotations", json=annotation)).status_code == 201
        assert (await http.post("/annotations", json={**annotation, "machine_id": m2})).status_code == 404
        assert (await http.post(f"/machines/{m2}/setup", json={"active": True})).status_code == 404
        assert (await http.get("/reasons", params={"machine_id": m1})).status_code == 200
        assert (await http.get("/reasons", params={"machine_id": m2})).status_code == 404
        # a reason of another organization cannot be attached even to the own machine
        foreign = {**annotation, "reason_id": world.b.unplanned_reason_id}
        assert (await http.post("/annotations", json=foreign)).status_code == 422
        # nothing administrative
        for path in ("/users", "/device-tokens", "/collectors", "/timeline?from=" + FROM.replace("+", "%2B")):
            assert (await http.get(path)).status_code == 403, path
        for name in ("machines", "programs", "shift-templates", "areas", "collectors", "state-rules"):
            assert (await http.get(f"/admin/{name}")).json() == [], name
        # of the structure a kiosk learns only the name of its own site
        assert [row["id"] for row in (await http.get("/admin/sites")).json()] == [world.a1.site_id]
        assert (await http.put(f"/admin/machines/{m1}", json={"name": "x"})).status_code in (403, 404)
        assert (await http.post("/recalc")).status_code == 403

    async with client(world.tokens["display_a"]) as http:  # TV dashboard of the first area
        assert await get_ids(http) == {m1}
        assert (await http.get("/auth/me")).json()["kiosk"] == {"machine_id": None, "area_id": world.a1.area_id}
        assert (await http.post("/scrap", json={"machine_id": m1, "qty": 1})).status_code == 403  # a TV enters nothing

    # the token is stored only as a hash; a revoked token stops working at once
    async with session_factory() as session:
        stored = (
            await session.execute(
                text("SELECT token_hash FROM device_tokens WHERE id = :i"), {"i": world.ids["token_kiosk_a"]}
            )
        ).scalar_one()
    assert stored == hash_token(world.tokens["kiosk_a"]) and world.tokens["kiosk_a"] not in stored
    async with client(world.tokens["admin_a"]) as http:
        listed = (await http.get("/device-tokens")).json()
        assert {t["name"] for t in listed} == {"acme kiosk m1", "acme display"} and "token" not in listed[0]
        issued = (await http.post("/device-tokens", json={"name": "new kiosk", "machine_id": m2})).json()
        assert (await http.delete(f"/device-tokens/{world.ids['token_kiosk_a']}")).status_code == 204
    async with client(world.tokens["kiosk_a"]) as http:
        assert (await http.get("/machines")).status_code == 401
    async with client(issued["token"]) as http:
        assert await get_ids(http) == {m2}


async def test_roles_are_granted_only_inside_the_own_organization(world: World) -> None:
    a, b = world.a1, world.b
    async with client(world.tokens["admin_a"]) as http:
        logins = {u["login"] for u in (await http.get("/users")).json()}
        assert logins == {
            "acme-admin",
            "acme-engineer",
            "acme-master",
            "acme-operator",
        }  # no B, no service, no superadmin
        new = {
            "login": "acme-new",
            "password": PASSWORD,
            "membership": {"role": "master", "org_id": a.org_id, "site_id": a.site_id},
        }
        created = await http.post("/users", json=new)
        assert created.status_code == 201
        user_id = created.json()["id"]
        grant = f"/users/{user_id}/memberships"
        assert (
            await http.post(
                grant, json={"role": "operator", "org_id": a.org_id, "site_id": a.site_id, "area_id": a.area_id}
            )
        ).status_code == 201
        assert (await http.post(grant, json={"role": "admin", "org_id": b.org_id})).status_code == 404
        assert (
            await http.post(grant, json={"role": "service"})
        ).status_code == 403  # only the superadmin appoints service
        assert (
            await http.post(grant, json={"role": "master", "org_id": a.org_id, "site_id": b.site_id})
        ).status_code == 422
        assert (await http.post(grant, json={"role": "root", "org_id": a.org_id})).status_code == 422
        assert (await http.post("/users", json={**new, "login": "acme-new"})).status_code == 409
        assert (await http.post("/admin/organizations", json={"code": "neworg", "name": "x"})).status_code == 403

    async with client(world.tokens["superadmin"]) as http:
        assert len((await http.get("/users")).json()) == 8
        assert (await http.post("/admin/organizations", json={"code": "neworg", "name": "New"})).status_code == 201
        assert (await http.post(f"/users/{user_id}/memberships", json={"role": "service"})).status_code == 201

    # a disabled user is locked out even with a valid session token
    async with client(world.tokens["admin_a"]) as http:
        assert (await http.put(f"/users/{world.ids['user_master_a1']}", json={"is_active": False})).status_code == 200
    async with client(world.tokens["master_a1"]) as http:
        assert (await http.get("/machines")).status_code == 401


async def log_in(login: str, password: str) -> str | None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as http:
        response = await http.post("/auth/login", json={"username": login, "password": password})
    return str(response.json()["token"]) if response.status_code == 200 else None


async def roles_of(world: World, login: str) -> list[dict[str, Any]]:
    """Every role of a user, as the superadmin sees it."""
    async with client(world.tokens["superadmin"]) as http:
        users = (await http.get("/users")).json()
    memberships: list[dict[str, Any]] = next(u["memberships"] for u in users if u["login"] == login)
    return memberships


async def test_site_admin_cannot_change_the_organization_admin(world: World) -> None:
    """An account is changed only by someone who administers every role of it."""
    a1, a2 = world.a1, world.a2
    async with client(world.tokens["admin_a"]) as http:
        site_admin = {"role": "admin", "org_id": a1.org_id, "site_id": a1.site_id}
        assert (
            await http.post("/users", json={"login": "acme-site-admin", "password": PASSWORD, "membership": site_admin})
        ).status_code == 201
        # the engineer of site a1 also works at site a2
        grant = {"role": "operator", "org_id": a2.org_id, "site_id": a2.site_id, "area_id": a2.area_id}
        assert (await http.post(f"/users/{world.ids['user_engineer_a1']}/memberships", json=grant)).status_code == 201
    token = await log_in("acme-site-admin", PASSWORD)
    assert token is not None
    org_admin, engineer, master = (world.ids[f"user_{k}"] for k in ("admin_a", "engineer_a1", "master_a1"))
    (org_admin_role,) = await roles_of(world, "acme-admin")
    engineer_roles = await roles_of(world, "acme-engineer")

    async with client(token) as http:
        users = {u["login"]: u for u in (await http.get("/users")).json()}
        assert users["acme-admin"]["manageable"] is False
        assert users["acme-engineer"]["manageable"] is False  # one of the roles is at another site
        assert users["acme-master"]["manageable"] is True
        for target in (org_admin, engineer):
            for change in ({"password": "taken-over"}, {"is_active": False}, {"full_name": "renamed"}):
                assert (await http.put(f"/users/{target}", json=change)).status_code == 403, (target, change)
        assert (await http.delete(f"/memberships/{org_admin_role['id']}")).status_code == 403
        # the engineer's role at the own site may not be taken away either: the account is not fully the caller's
        own_site_role = next(m for m in engineer_roles if m["site_id"] == a1.site_id)
        assert (await http.delete(f"/memberships/{own_site_role['id']}")).status_code == 403
        # a user whose every role is on the own site is the site administrator's
        assert (await http.put(f"/users/{master}", json={"password": "new-master-password"})).status_code == 200

    assert await log_in("acme-admin", PASSWORD) is not None
    assert await log_in("acme-admin", "taken-over") is None
    assert await log_in("acme-engineer", PASSWORD) is not None
    assert await log_in("acme-master", "new-master-password") is not None
    assert await roles_of(world, "acme-admin") == [org_admin_role]
    assert await roles_of(world, "acme-engineer") == engineer_roles
    async with client(world.tokens["admin_a"]) as http:
        users = {u["login"]: u for u in (await http.get("/users")).json()}
        assert users["acme-admin"]["full_name"] == "acme-admin" and users["acme-admin"]["is_active"]
        # the organization administrator holds all of the engineer's roles
        assert users["acme-engineer"]["manageable"] is True
        assert (await http.put(f"/users/{engineer}", json={"password": "by-org-admin"})).status_code == 200
    assert await log_in("acme-engineer", "by-org-admin") is not None


async def test_admin_of_b_cannot_change_a_user_who_also_works_for_a(world: World) -> None:
    b = world.b
    shared = world.ids["user_engineer_a1"]
    (role_a,) = await roles_of(world, "acme-engineer")
    async with client(world.tokens["superadmin"]) as http:
        grant = {"role": "operator", "org_id": b.org_id, "site_id": b.site_id, "area_id": b.area_id}
        granted = await http.post(f"/users/{shared}/memberships", json=grant)
        assert granted.status_code == 201
    role_b = granted.json()["id"]

    async with client(world.tokens["admin_b"]) as http:
        users = {u["login"]: u for u in (await http.get("/users")).json()}
        assert users["acme-engineer"]["manageable"] is False
        assert [m["id"] for m in users["acme-engineer"]["memberships"]] == [role_b]  # roles in A stay hidden
        for change in ({"password": "taken-over"}, {"is_active": False}, {"full_name": "renamed"}):
            assert (await http.put(f"/users/{shared}", json=change)).status_code == 403, change
        assert (await http.delete(f"/memberships/{role_b}")).status_code == 403
        assert (await http.delete(f"/memberships/{role_a['id']}")).status_code == 404
    # nor may the administrator of A: the user also works for B
    async with client(world.tokens["admin_a"]) as http:
        users = {u["login"]: u for u in (await http.get("/users")).json()}
        assert users["acme-engineer"]["manageable"] is False
        assert [m["id"] for m in users["acme-engineer"]["memberships"]] == [role_a["id"]]
        assert (await http.put(f"/users/{shared}", json={"password": "taken-over"})).status_code == 403
        assert (await http.put(f"/users/{shared}", json={"is_active": False})).status_code == 403
        assert (await http.delete(f"/memberships/{role_a['id']}")).status_code == 403

    assert await log_in("acme-engineer", PASSWORD) is not None
    assert await log_in("acme-engineer", "taken-over") is None
    assert [m["id"] for m in await roles_of(world, "acme-engineer")] == [role_a["id"], role_b]
    async with client(world.tokens["engineer_a1"]) as http:
        assert (await http.get("/machines")).status_code == 200  # still active
    # the superadmin administers every account
    async with client(world.tokens["superadmin"]) as http:
        assert (await http.put(f"/users/{shared}", json={"password": "by-superadmin"})).status_code == 200
    assert await log_in("acme-engineer", "by-superadmin") is not None


async def test_site_references_override_organization_defaults(world: World, session_factory: Factory) -> None:
    a = world.a1
    async with client(world.tokens["admin_a"]) as http:
        default = (await http.get("/reasons", params={"site_id": a.site_id})).json()
        assert [g["code"] for g in default] == ["PLAN"]
        own = {"org_id": a.org_id, "site_id": a.site_id, "code": "LOCAL", "name": "acme a1 own reason"}
        assert (await http.post("/admin/downtime-reasons", json=own)).status_code == 201
        # the site now uses its own list; the other site of the organization still uses the defaults
        assert [g["code"] for g in (await http.get("/reasons", params={"site_id": a.site_id})).json()] == ["LOCAL"]
        assert [g["code"] for g in (await http.get("/reasons", params={"site_id": world.a2.site_id})).json()] == [
            "PLAN"
        ]
        # a row may not point across organizations
        cross = {"org_id": a.org_id, "code": "CROSS", "name": "x", "parent_id": world.b.group_id}
        assert (await http.post("/admin/downtime-reasons", json=cross)).status_code == 422
        cross_program = {"org_id": a.org_id, "program": "O1", "ideal_cycle_time_s": 5, "machine_id": world.b.machine_id}
        assert (await http.post("/admin/programs", json=cross_program)).status_code == 422
        assert (
            await http.put(f"/admin/machines/{a.machine_id}", json={"name": "acme m1", "area_id": world.b.area_id})
        ).status_code == 422


# ---------------------------------------------------------------- WebSocket


class FakePubSub:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis

    async def __aenter__(self) -> FakePubSub:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def subscribe(self, *channels: str) -> None:
        self._redis.subscribed.update(channels)

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            channel, data = await self._redis.queue.get()
            if channel in self._redis.subscribed:
                yield {"type": "message", "channel": channel, "data": data}


class FakeRedis:
    """Just enough of Redis for /ws/live: snapshots by key and Pub/Sub by channel."""

    def __init__(self, snapshots: dict[str, str]) -> None:
        self.snapshots = snapshots
        self.subscribed: set[str] = set()
        self.queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.snapshots.get(k) for k in keys]

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)


class FakeWebSocket:
    def __init__(self) -> None:
        self.app = app
        self.sent: list[dict[str, Any]] = []
        self.closed_with: int | None = None
        self._hold = asyncio.Event()

    async def accept(self) -> None:
        return None

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def receive_text(self) -> str:
        await self._hold.wait()
        return ""


async def test_websocket_delivers_only_machines_in_scope(world: World) -> None:
    seeds = [world.a1, world.a1_second, world.a2, world.b]
    payloads = {s.machine_id: json.dumps({"machine_id": s.machine_id, "name": s.machine["name"]}) for s in seeds}
    channels = {s.machine_id: live_channel(s.machine["org_code"], s.machine["site_code"]) for s in seeds}

    async def connect(token: str) -> tuple[FakeWebSocket, FakeRedis, asyncio.Task[None]]:
        redis = FakeRedis({live_key(m): p for m, p in payloads.items()})
        app.state.redis = redis
        socket = FakeWebSocket()
        task = asyncio.create_task(ws_live(socket, token=token))  # type: ignore[arg-type]
        await asyncio.sleep(0.3)
        return socket, redis, task

    async def updates(token: str) -> tuple[set[int], set[str], set[int]]:
        """(machines in the snapshot, subscribed channels, machines whose updates arrived)."""
        socket, redis, task = await connect(token)
        for machine_id, payload in payloads.items():  # every machine of the installation changes
            await redis.queue.put((channels[machine_id], payload))
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        snapshot = {m["machine_id"] for m in socket.sent[0]["machines"]}
        return snapshot, redis.subscribed, {m["machine"]["machine_id"] for m in socket.sent[1:]}

    m1, m2, m3, mb = (s.machine_id for s in seeds)
    try:
        assert await updates(world.tokens["admin_b"]) == ({mb}, {"live.bravo.b1"}, {mb})
        assert await updates(world.tokens["admin_a"]) == ({m1, m2, m3}, {"live.acme.a1", "live.acme.a2"}, {m1, m2, m3})
        # a master shares the site channel with the neighbouring area but receives only the own machines
        assert await updates(world.tokens["master_a1"]) == ({m1}, {"live.acme.a1"}, {m1})
        assert await updates(world.tokens["engineer_a1"]) == ({m1, m2}, {"live.acme.a1"}, {m1, m2})
        assert await updates(world.tokens["kiosk_a"]) == ({m1}, {"live.acme.a1"}, {m1})
        assert await updates(world.tokens["service"]) == ({m1, m2, m3, mb}, set(channels.values()), {m1, m2, m3, mb})

        socket, redis, task = await connect("garbage")
        await task
        assert socket.closed_with == 4401 and socket.sent == [] and redis.subscribed == set()
    finally:
        app.state.redis = None
