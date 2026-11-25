"""QR passport of a machine (ADR 0009): where a sticker leads, reissuing its code, PDF stickers."""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import identity
from app.admin.passport import CODE
from app.api.main import app
from app.core.config import Settings
from app.ingest.service import IngestService
from tests.conftest import Seed, encode, make_message, seed_reference

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings()
PASSWORD = "secret-password"
NOT_FOUND = (404, {"detail": "not found"})
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass
class Plant:
    a: Seed  # organization A, site a1, first area: machine m1
    a2: Seed  # organization A, site a2: machine m3
    b: Seed  # organization B: machine m1
    m2: int  # organization A, site a1, second area
    tokens: dict[str, str] = field(default_factory=dict)
    codes: dict[str, str] = field(default_factory=dict)  # m1, m2, m3, b -> passport code


async def codes_by_id(factory: Factory) -> dict[int, str]:
    async with factory() as session:
        rows = await session.execute(text("SELECT id, passport_code FROM machines"))
        return {r.id: r.passport_code for r in rows}


@pytest.fixture
async def plant(session_factory: Factory) -> Plant:
    a = await seed_reference(session_factory, org="acme", site="a1", machine="m1", collector="col-a1")
    a2 = await seed_reference(session_factory, org="acme", site="a2", machine="m3", collector="col-a2")
    b = await seed_reference(session_factory, org="bravo", site="b1", machine="m1", collector="col-b1")
    async with session_factory() as session, session.begin():

        async def one(sql: str, **params: object) -> int:
            return int((await session.execute(text(sql), params)).scalar_one())

        area2 = await one("INSERT INTO areas (site_id, name) VALUES (:s, 'a1 second') RETURNING id", s=a.site_id)
        m2 = await one(
            "INSERT INTO machines (site_id, area_id, code, name, collector_id) "
            "VALUES (:s, :a, 'm2', 'acme m2', 'col-a1') RETURNING id",
            s=a.site_id,
            a=area2,
        )
        plant = Plant(a, a2, b, m2)
        await identity.bootstrap(session, SETTINGS)
        users = {
            "admin_a": ("acme-admin", "admin", a.org_id, None, None),
            "admin_b": ("bravo-admin", "admin", b.org_id, None, None),
            "engineer_a1": ("acme-engineer", "engineer", a.org_id, a.site_id, None),
            "master_a1": ("acme-master", "master", a.org_id, a.site_id, a.area_id),
            "operator_a1": ("acme-operator", "operator", a.org_id, a.site_id, a.area_id),
            "service": ("service-engineer", "service", None, None, None),
        }
        for login, role, org, site, area in users.values():
            user_id = await identity.create_user(session, login, PASSWORD, login)
            await identity.add_membership(session, user_id, role, org, site, area)
        logins = {key: (user[0], PASSWORD) for key, user in users.items()}
        logins["superadmin"] = (SETTINGS.admin_username, SETTINGS.admin_password)
        for key, (login, password) in logins.items():
            token = await identity.login(session, SETTINGS, login, password)
            assert token is not None
            plant.tokens[key] = token
        _, plant.tokens["kiosk_m1"] = await identity.issue_device_token(session, "kiosk m1", a.machine_id, None, None)
        _, plant.tokens["display_a1"] = await identity.issue_device_token(session, "display", None, a.area_id, None)
        _, plant.tokens["kiosk_b"] = await identity.issue_device_token(session, "kiosk b", b.machine_id, None, None)
    codes = await codes_by_id(session_factory)
    plant.codes = {"m1": codes[a.machine_id], "m2": codes[m2], "m3": codes[a2.machine_id], "b": codes[b.machine_id]}
    app.state.settings = SETTINGS
    app.state.session_factory = session_factory
    app.state.redis = None
    return plant


@contextlib.asynccontextmanager
async def client(token: str | None, headers: dict[str, str] | None = None) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1", headers=sent) as http:
        yield http


async def answer(token: str | None, code: str) -> tuple[int, object]:
    async with client(token) as http:
        response = await http.get(f"/passport/{code}")
    return response.status_code, response.json()


def pages(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", pdf))


# ---------------------------------------------------------------- the code


async def test_every_machine_gets_its_own_code(plant: Plant, session_factory: Factory, service: IngestService) -> None:
    """Machines created by hand and those registered by ingest from incoming data alike."""
    message = make_message("telemetry", collector_id="col-a1", machine_id="m7")
    result = await service.handle_batch([encode(message, org="acme", site="a1")])
    assert result.telemetry_inserted == 1
    codes = await codes_by_id(session_factory)
    assert len(codes) == 5  # m1, m3, bravo m1, m2 and m7 registered by ingest
    assert all(CODE.fullmatch(code) for code in codes.values())
    assert len(set(codes.values())) == 5

    # the generator: 8 characters of the alphabet without look-alikes (no 0/O, no 1/I), every one in use
    async with session_factory() as session:
        sample = (await session.execute(text("SELECT new_passport_code() FROM generate_series(1, 2000)"))).scalars()
        generated = list(sample)
    assert all(CODE.fullmatch(code) for code in generated)
    assert len(set(generated)) == len(generated)
    assert set("".join(generated)) == set(ALPHABET)


# ---------------------------------------------------------------- where the sticker leads


async def test_sticker_leads_each_role_to_its_page(plant: Plant) -> None:
    m1 = plant.a.machine_id
    expected = {
        "master_a1": "machine",  # status, timeline, repair request
        "engineer_a1": "machine",
        "admin_a": "machine",
        "service": "service",  # machine card of the service
        "superadmin": "service",
        "operator_a1": "operator",  # operator screen of the machine
        "kiosk_m1": "operator",
    }
    for who, view in expected.items():
        assert await answer(plant.tokens[who], plant.codes["m1"]) == (200, {"machine_id": m1, "view": view}), who
    # typed by hand from the sticker: case and spaces do not matter
    assert await answer(plant.tokens["master_a1"], f" {plant.codes['m1'].lower()} ") == (
        200,
        {"machine_id": m1, "view": "machine"},
    )


async def test_no_access_and_a_wrong_code_look_the_same(plant: Plant) -> None:
    """Another organization, a machine outside the caller's nodes and a code that does not exist: one answer."""
    codes = plant.codes
    unknown = next(c for c in ("ZZZZZZZZ", "YYYYYYYY") if c not in codes.values())
    cases = [
        ("admin_b", codes["m1"]),  # another organization
        ("kiosk_b", codes["m1"]),
        ("admin_a", codes["b"]),
        ("master_a1", codes["m2"]),  # another area of the master's site
        ("operator_a1", codes["m2"]),
        ("engineer_a1", codes["m3"]),  # another site of the organization
        ("kiosk_m1", codes["m2"]),  # a kiosk opens its own machine only
        ("display_a1", codes["m1"]),  # a TV of the area has no page for a machine
        ("admin_a", unknown),
        ("superadmin", unknown),
        ("admin_a", "abc"),
        ("admin_a", "O0I1O0I1"),  # look-alikes are never in a code
        ("admin_a", "x" * 300),
    ]
    for who, code in cases:
        assert await answer(plant.tokens[who], code) == NOT_FOUND, (who, code)


async def test_opening_a_sticker_needs_signing_in(plant: Plant) -> None:
    """Without a session: 401 (the web page sends to the login form and back), whatever the code is."""
    for code in (plant.codes["m1"], "ZZZZZZZZ", "abc"):
        assert (await answer(None, code))[0] == 401
        assert (await answer("garbage", code))[0] == 401


# ---------------------------------------------------------------- reissue


async def test_reissued_code_disables_the_old_sticker(plant: Plant, session_factory: Factory) -> None:
    m1, old = plant.a.machine_id, plant.codes["m1"]
    before = await codes_by_id(session_factory)
    for who, status in (
        ("master_a1", 403),
        ("engineer_a1", 403),
        ("operator_a1", 403),
        ("service", 403),
        ("kiosk_m1", 403),
        ("admin_b", 404),
    ):
        async with client(plant.tokens[who]) as http:
            assert (await http.post(f"/admin/machines/{m1}/passport")).status_code == status, who
    assert await codes_by_id(session_factory) == before

    async with client(plant.tokens["admin_a"]) as http:
        response = await http.post(f"/admin/machines/{m1}/passport")
        assert response.status_code == 200
        new = response.json()["passport_code"]
        assert new != old and CODE.fullmatch(new)
        machines = {row["id"]: row for row in (await http.get("/admin/machines")).json()}
        assert machines[m1]["passport_code"] == new
    for who in ("admin_a", "master_a1", "service"):
        assert await answer(plant.tokens[who], old) == NOT_FOUND, who
        assert (await answer(plant.tokens[who], new))[0] == 200, who
    after = await codes_by_id(session_factory)
    assert {k: v for k, v in after.items() if k != m1} == {k: v for k, v in before.items() if k != m1}


async def test_admin_cannot_set_a_code_by_hand(plant: Plant, session_factory: Factory) -> None:
    """Editing a machine in the admin UI leaves its code alone: a new one comes only from reissue."""
    m1 = plant.a.machine_id
    async with client(plant.tokens["admin_a"]) as http:
        response = await http.put(f"/admin/machines/{m1}", json={"name": "Фрезерный 1", "passport_code": "AAAAAAAA"})
        assert response.status_code == 200
    assert (await codes_by_id(session_factory))[m1] == plant.codes["m1"]


# ---------------------------------------------------------------- PDF stickers


async def test_sticker_of_a_machine_is_a_pdf_with_the_link(plant: Plant) -> None:
    m1, code = plant.a.machine_id, plant.codes["m1"]
    async with client(plant.tokens["admin_a"]) as http:
        response = await http.get(f"/admin/machines/{m1}/passport.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="passport-m1.pdf"'
    pdf = response.content
    assert pdf.startswith(b"%PDF-") and pdf.rstrip().endswith(b"%%EOF")
    assert pages(pdf) == 1
    # the link is the address the administrator used; it carries nothing but the code
    assert f"(http://test/m/{code})".encode() in pdf
    assert plant.tokens["admin_a"].encode() not in pdf

    # behind a reverse proxy: the address the browser used (the first one of a chain)
    proxy = {"X-Forwarded-Host": "cnc.plant.local:8080, 10.0.0.5", "X-Forwarded-Proto": "https,http"}
    async with client(plant.tokens["admin_a"], proxy) as http:
        pdf = (await http.get(f"/admin/machines/{m1}/passport.pdf")).content
    assert f"(https://cnc.plant.local:8080/m/{code})".encode() in pdf

    # MM_PUBLIC_URL wins over the request
    app.state.settings = SETTINGS.model_copy(update={"public_url": "https://mm.example.com/"})
    try:
        async with client(plant.tokens["admin_a"], proxy) as http:
            pdf = (await http.get(f"/admin/machines/{m1}/passport.pdf")).content
    finally:
        app.state.settings = SETTINGS
    assert f"(https://mm.example.com/m/{code})".encode() in pdf

    for who, status in (("master_a1", 403), ("engineer_a1", 403), ("service", 403), ("admin_b", 404)):
        async with client(plant.tokens[who]) as http:
            assert (await http.get(f"/admin/machines/{m1}/passport.pdf")).status_code == status, who


async def test_sheet_of_an_area_has_every_active_machine(plant: Plant, session_factory: Factory) -> None:
    a = plant.a
    async with session_factory() as session, session.begin():
        for i in range(11):
            await session.execute(
                text(
                    "INSERT INTO machines (site_id, area_id, code, name, collector_id, sort_order) "
                    "VALUES (:s, :a, :c, :n, 'col-a1', :i)"
                ),
                {"s": a.site_id, "a": a.area_id, "c": f"x{i:02}", "n": f"Токарный станок с ЧПУ № {i}", "i": i},
            )
        await session.execute(
            text(
                "INSERT INTO machines (site_id, area_id, code, name, collector_id, is_active) "
                "VALUES (:s, :a, 'off', 'Списанный', 'col-a1', false)"
            ),
            {"s": a.site_id, "a": a.area_id},
        )
        empty_area = (
            await session.execute(
                text("INSERT INTO areas (site_id, name) VALUES (:s, 'empty') RETURNING id"), {"s": a.site_id}
            )
        ).scalar_one()
        rows = await session.execute(
            text("SELECT code, passport_code, is_active FROM machines WHERE area_id = :a"), {"a": a.area_id}
        )
        area_codes = {r.code: (r.passport_code, r.is_active) for r in rows}
    assert len(area_codes) == 13

    async with client(plant.tokens["admin_a"]) as http:
        response = await http.get(f"/admin/areas/{a.area_id}/passports.pdf")
        assert (await http.get(f"/admin/areas/{empty_area}/passports.pdf")).status_code == 422
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    pdf = response.content
    assert pdf.startswith(b"%PDF-")
    assert pages(pdf) == 2  # 12 active machines, ten stickers to an A4 page
    for machine, (code, active) in area_codes.items():
        assert (f"(http://test/m/{code})".encode() in pdf) == active, machine
    assert plant.codes["m2"].encode() not in pdf  # the second area

    for who, status in (("master_a1", 403), ("engineer_a1", 403), ("display_a1", 403), ("admin_b", 404)):
        async with client(plant.tokens[who]) as http:
            assert (await http.get(f"/admin/areas/{a.area_id}/passports.pdf")).status_code == status, who
