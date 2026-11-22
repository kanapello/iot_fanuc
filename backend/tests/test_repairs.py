"""Service repairs on a real PostgreSQL + TimescaleDB, through the HTTP API (ADR 0006):
alarm journal, repair requests with times from the timeline, maintenance by running hours."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import identity
from app.api.main import app
from app.core.config import Settings
from app.worker.__main__ import Worker
from tests.conftest import Seed, put_samples, seed_reference

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings()
NOW = datetime.now(UTC)
T0 = NOW.replace(second=0, microsecond=0) - timedelta(hours=3)
PASSWORD = "secret-password"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@dataclass
class Plant:
    m1: Seed  # site p1
    m2: Seed  # site p2 of the same organization
    tokens: dict[str, str] = field(default_factory=dict)
    users: dict[str, int] = field(default_factory=dict)


@pytest.fixture
async def plant(session_factory: Factory) -> Plant:
    m1 = await seed_reference(session_factory, org="plant", site="p1", machine="m1", collector="col-p1")
    m2 = await seed_reference(session_factory, org="plant", site="p2", machine="m2", collector="col-p2")
    result = Plant(m1, m2)
    async with session_factory() as session, session.begin():
        await identity.bootstrap(session, SETTINGS)
        people = {
            "operator": ("operator", m1.org_id, m1.site_id, m1.area_id, "Оператор"),
            "master": ("master", m1.org_id, m1.site_id, m1.area_id, "Мастер"),
            "engineer": ("engineer", m1.org_id, m1.site_id, None, "Технолог"),
            "orgadmin": ("admin", m1.org_id, None, None, "Администратор"),
            "service": ("service", None, None, None, "Петров П. П."),
            "service2": ("service", None, None, None, "Сидоров С. С."),
        }
        for login, (role, org, site, area, name) in people.items():
            user_id = await identity.create_user(session, login, PASSWORD, name)
            await identity.add_membership(session, user_id, role, org, site, area)
            token = await identity.login(session, SETTINGS, login, PASSWORD)
            assert token is not None
            result.tokens[login], result.users[login] = token, user_id
        _, result.tokens["kiosk"] = await identity.issue_device_token(session, "kiosk m1", m1.machine_id, None, None)
        superadmin = await identity.login(session, SETTINGS, SETTINGS.admin_username, SETTINGS.admin_password)
        assert superadmin is not None
        result.tokens["superadmin"] = superadmin
    app.state.settings = SETTINGS
    app.state.session_factory = session_factory
    app.state.redis = None
    return result


@contextlib.asynccontextmanager
async def client(token: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1", headers=headers) as http:
        yield http


async def put_events(factory: Factory, machine_id: int, events: list[tuple[datetime, str, str | None]]) -> None:
    """Raw machine events: (ts, type, alarm code); the message is derived from the code."""
    messages = {"SV0401": "V-READY OFF", "EX1000": "EMERGENCY STOP", "OT0500": "OVER TRAVEL"}
    rows = [
        {
            "msg_id": uuid.uuid4(),
            "ts": ts,
            "m": machine_id,
            "seq": i + 1,
            "type": kind,
            "code": code,
            "message": messages.get(code or ""),
            "payload": f'{{"type": "{kind}", "category": "servo"}}',
        }
        for i, (ts, kind, code) in enumerate(events)
    ]
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO machine_events (msg_id, ts, machine_id, collector_id, seq, type, code, message, payload) "
                "VALUES (:msg_id, :ts, :m, 'c', :seq, :type, :code, :message, CAST(:payload AS jsonb))"
            ),
            rows,
        )


async def build_timeline(factory: Factory, seed: Seed, samples: list[dict[str, Any]]) -> None:
    await put_samples(factory, seed.machine_id, samples, collector=seed.machine["collector_id"])
    worker = Worker(SETTINGS, factory)
    for _ in range(3):
        await worker.rebuild_timeline(seed.machine, NOW)


def run_samples(segments: list[tuple[float, float, str]]) -> list[dict[str, Any]]:
    """Samples every 10 s over [start, end) minutes: RUN, STOP or ALARM."""
    samples = []
    for start, end, state in segments:
        t = start * 60
        while t < end * 60:
            samples.append(
                {
                    "ts": T0 + timedelta(seconds=t),
                    "run": "START" if state == "RUN" else "STOP",
                    "alarm": state == "ALARM",
                }
            )
            t += 10
    return samples


async def stored(factory: Factory, sql: str, **params: Any) -> list[Any]:
    async with factory() as session:
        return list((await session.execute(text(sql), params)).all())


# ---------------------------------------------------------------- alarm journal


async def test_alarm_journal_pairs_events_and_shows_the_telemetry_before(
    plant: Plant, session_factory: Factory
) -> None:
    m1, m2 = plant.m1.machine_id, plant.m2.machine_id
    # alarm only between 30 and 35 min and between 40 and 41 min; with load values to look at
    samples = run_samples([(0, 30, "RUN"), (30, 35, "ALARM"), (35, 40, "STOP"), (40, 41, "ALARM"), (41, 60, "STOP")])
    for i, sample in enumerate(samples):
        sample["spindle_load"] = float(i)
    await put_samples(session_factory, m1, samples, collector="col-p1")
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE telemetry SET spindle_load = (EXTRACT(EPOCH FROM ts - :t0) / 10) WHERE machine_id = :m"),
            {"t0": T0, "m": m1},
        )
    await put_events(
        session_factory,
        m1,
        [
            (at(25), "program_changed", None),
            (at(30), "alarm_raised", "SV0401"),
            (at(35), "alarm_cleared", "SV0401"),
            (at(38), "alarm_cleared", "SV0401"),  # a clearing without a raise: ignored
            (at(40), "alarm_raised", "EX1000"),  # its clearing was lost: ends with the first sample without alarm
            (at(50), "alarm_raised", "SV0401"),
            (at(52), "alarm_raised", "SV0401"),  # announced again after a collector restart: the same episode
            (at(55), "alarm_cleared", "SV0401"),
            (NOW - timedelta(minutes=1), "alarm_raised", "OT0500"),  # still active
        ],
    )
    await put_events(session_factory, m2, [(at(10), "alarm_raised", "SV0401"), (at(11), "alarm_cleared", "SV0401")])

    async with client(plant.tokens["engineer"]) as http:
        journal = (await http.get("/alarms", params={"from": T0.isoformat(), "machine_id": m1})).json()
        assert [(e["code"], e["start_ts"][:16], e["active"]) for e in journal] == [
            ("OT0500", (NOW - timedelta(minutes=1)).isoformat()[:16], True),
            ("SV0401", at(50).isoformat()[:16], False),
            ("EX1000", at(40).isoformat()[:16], False),
            ("SV0401", at(30).isoformat()[:16], False),
        ]
        by_start = {e["start_ts"][:16]: e for e in journal}
        assert by_start[at(50).isoformat()[:16]]["duration_s"] == 300
        assert by_start[at(30).isoformat()[:16]]["duration_s"] == 300
        assert by_start[at(30).isoformat()[:16]]["message"] == "V-READY OFF"
        assert by_start[at(30).isoformat()[:16]]["category"] == "servo"
        assert by_start[at(40).isoformat()[:16]]["duration_s"] == 60  # first sample without alarm at 41 min
        active_for = (datetime.now(UTC) - (NOW - timedelta(minutes=1))).total_seconds()
        assert journal[0]["duration_s"] == pytest.approx(active_for, abs=5)  # active: up to now
        assert {e["machine_name"] for e in journal} == {"plant m1"}
        assert all(e["request_id"] is None for e in journal)

        only = (await http.get("/alarms", params={"from": T0.isoformat(), "machine_id": m1, "code": "SV0401"})).json()
        assert len(only) == 2
        later = (await http.get("/alarms", params={"from": at(45).isoformat(), "machine_id": m1})).json()
        assert [e["code"] for e in later] == ["OT0500", "SV0401"]  # episodes that started in the period

        frequency = (await http.get("/alarms/frequency", params={"from": T0.isoformat()})).json()
        assert [(r["machine_name"], r["count"], r["active"]) for r in frequency] == [("plant m1", 4, 1)]
        row = frequency[0]
        assert row["per_day"] == pytest.approx(4 / ((datetime.now(UTC) - T0).total_seconds() / 86400), rel=0.01)
        codes = {c["code"]: (c["count"], c["duration_s"]) for c in row["codes"]}
        assert row["codes"][0]["code"] == "SV0401"  # the most frequent first
        assert (codes["SV0401"], codes["EX1000"], codes["OT0500"][0]) == ((2, 600), (1, 60), 1)

        context = (await http.get(f"/machines/{m1}/alarm-context", params={"ts": at(30).isoformat()})).json()
        stamps = [datetime.fromisoformat(s["ts"]) for s in context["telemetry"]]
        assert stamps[0] == at(20) - timedelta(seconds=10)  # the state the window starts in
        assert stamps[1] == at(20) and stamps[-1] == at(30)
        assert stamps == sorted(stamps) and len(stamps) == 62
        assert context["telemetry"][-1]["spindle_load"] == 180  # 30 min = 180 samples of 10 s
        assert [(e["type"], e["code"]) for e in context["events"]] == [
            ("program_changed", None),
            ("alarm_raised", "SV0401"),
        ]

    # the engineer of site p1 does not see site p2; the organization administrator does
    async with client(plant.tokens["orgadmin"]) as http:
        frequency = (await http.get("/alarms/frequency", params={"from": T0.isoformat()})).json()
        assert {r["machine_name"]: r["count"] for r in frequency} == {"plant m1": 4, "plant m2": 1}
    # the journal is analytics: not for the operator and the kiosk
    for who in ("operator", "kiosk"):
        async with client(plant.tokens[who]) as http:
            assert (await http.get("/alarms", params={"from": T0.isoformat()})).status_code == 403, who
            context_call = await http.get(f"/machines/{m1}/alarm-context", params={"ts": at(30).isoformat()})
            assert context_call.status_code == 403, who


# ---------------------------------------------------------------- repair requests


async def test_request_from_an_alarm_goes_to_done_and_into_the_repair_log(
    plant: Plant, session_factory: Factory
) -> None:
    m1 = plant.m1.machine_id
    await build_timeline(
        session_factory,
        plant.m1,
        run_samples([(0, 30, "RUN"), (30, 35, "ALARM"), (35, 80, "STOP"), (80, 175, "RUN")]),
    )
    await put_events(session_factory, m1, [(at(30), "alarm_raised", "SV0401"), (at(35), "alarm_cleared", "SV0401")])
    async with client(plant.tokens["master"]) as http:
        menu = (await http.get("/auth/me")).json()["menu"]
        assert (menu["alarms"], menu["repairs"], menu["service"]) == (True, True, False)  # geometry stays hidden
        (alarm,) = (await http.get("/alarms", params={"from": T0.isoformat()})).json()
        one_click = {"machine_id": m1, "alarm_ts": alarm["start_ts"], "alarm_code": alarm["code"]}
        created = await http.post("/repair-requests", json=one_click)
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]
        assert (await http.post("/repair-requests", json=one_click)).status_code == 409  # one request per alarm
        wrong = {**one_click, "alarm_code": "XX0000"}
        assert (await http.post("/repair-requests", json=wrong)).status_code == 422
        assert (await http.post("/repair-requests", json={"machine_id": m1, "alarm_code": "SV0401"})).status_code == 422
        (alarm,) = (await http.get("/alarms", params={"from": T0.isoformat()})).json()
        assert alarm["request_id"] == request_id
        request = (await http.get(f"/repair-requests/{request_id}")).json()
        assert (request["status"], request["description"], request["created_by_name"]) == (
            "new",
            "SV0401 V-READY OFF",
            "Мастер",
        )
        assert datetime.fromisoformat(request["failure_ts"]) == at(30)
        # restoration by the timeline: back in RUN at 80 min, while the request is still new
        assert datetime.fromisoformat(request["restored_at"]) == at(80)
        assert request["restoration_s"] == 50 * 60
        assert request["reaction_s"] is None
        # only the service moves requests
        transition = await http.post(f"/repair-requests/{request_id}/status", json={"status": "accepted"})
        assert transition.status_code == 403
    async with client(plant.tokens["operator"]) as http:
        assert (await http.post("/repair-requests", json=one_click)).status_code == 403
        assert (await http.get("/repair-requests")).status_code == 403

    status_url = f"/repair-requests/{request_id}/status"
    async with client(plant.tokens["service"]) as http:
        engineers = (await http.get("/service-engineers")).json()
        assert [e["name"] for e in engineers] == ["Петров П. П.", "Сидоров С. С."]
        assert (await http.post(status_url, json={"status": "in_progress"})).status_code == 409  # not accepted yet
        assert (await http.post(status_url, json={"status": "accepted"})).status_code == 422  # who will do it?
        not_service = {"status": "accepted", "executor_id": plant.users["master"]}
        assert (await http.post(status_url, json=not_service)).status_code == 422
        accept = {"status": "accepted", "executor_id": plant.users["service2"], "comment": "выезд завтра"}
        assert (await http.post(status_url, json=accept)).status_code == 200
        reassign = {"status": "accepted", "executor_id": plant.users["service"]}
        assert (await http.post(status_url, json=reassign)).status_code == 200
        assert (await http.post(status_url, json={"status": "in_progress"})).status_code == 200
        assert (await http.post(status_url, json={"status": "done"})).status_code == 422  # what was done?
        done = {"status": "done", "work_done": "Замена кабеля энкодера оси X"}
        assert (await http.post(status_url, json=done)).status_code == 200
        assert (await http.post(status_url, json=done)).status_code == 409
        assert (await http.post(f"/repair-requests/{request_id}/void", json={"reason": "ошибка"})).status_code == 409

        request = (await http.get(f"/repair-requests/{request_id}")).json()
        assert (request["status"], request["executor_name"], request["work_done"]) == (
            "done",
            "Петров П. П.",
            "Замена кабеля энкодера оси X",
        )
        assert [h["action"] for h in request["history"]] == ["created", "accepted", "reassigned", "in_progress", "done"]
        assert request["history"][1]["comment"] == "выезд завтра"
        started = datetime.fromisoformat(request["started_at"])
        assert request["reaction_s"] == (started - at(30)).total_seconds()
        # the repair log of the service: written by the request, with a link to it
        (repair,) = (await http.get(f"/machines/{m1}/repairs")).json()
        assert (repair["request_id"], repair["description"], repair["engineer"]) == (
            request_id,
            "Замена кабеля энкодера оси X",
            "Петров П. П.",
        )
        # manual entry stays for work done outside the system
        outside = {"completed_at": at(10).isoformat(), "description": "Замена ремня шпинделя"}
        assert (await http.post(f"/machines/{m1}/repairs", json=outside)).status_code == 201
        logged = (await http.get(f"/machines/{m1}/repairs")).json()
        assert sorted((r["request_id"] is None) for r in logged) == [False, True]

    # the customer sees the request through to the end, with the executor and the times
    async with client(plant.tokens["master"]) as http:
        (row,) = (await http.get("/repair-requests")).json()
        assert (row["id"], row["status"], row["executor_name"]) == (request_id, "done", "Петров П. П.")
        assert row["restoration_s"] == 50 * 60 and row["reaction_s"] > 0
        assert row["machine_name"] == "plant m1"
        assert (await http.get(f"/machines/{m1}/repairs")).status_code == 404  # the log is the service archive
    history = await stored(
        session_factory, "SELECT count(*) FROM repair_request_history WHERE request_id = :r", r=request_id
    )
    assert history[0][0] == 5


async def test_manual_request_reaction_restoration_and_withdrawal(plant: Plant, session_factory: Factory) -> None:
    m1 = plant.m1.machine_id
    await build_timeline(session_factory, plant.m1, run_samples([(0, 60, "STOP"), (60, 175, "RUN")]))
    async with client(plant.tokens["master"]) as http:
        assert (await http.post("/repair-requests", json={"machine_id": m1})).status_code == 422  # what is wrong?
        future = {"machine_id": m1, "description": "Течь масла", "failure_ts": (NOW + timedelta(hours=1)).isoformat()}
        assert (await http.post("/repair-requests", json=future)).status_code == 422
        stopped = {"machine_id": m1, "description": "Не включается насос СОЖ", "failure_ts": at(20).isoformat()}
        running = {"machine_id": m1, "description": "Шум в шпинделе", "failure_ts": at(90).isoformat()}
        first = (await http.post("/repair-requests", json=stopped)).json()["id"]
        second = (await http.post("/repair-requests", json=running)).json()["id"]
        rows = {r["id"]: r for r in (await http.get("/repair-requests")).json()}
        assert rows[first]["restoration_s"] == 40 * 60  # stopped at the failure, RUN again at 60 min
        assert rows[second]["restoration_s"] == 0  # it was running: no downtime
        # the customer withdraws a request nobody has taken yet
        assert (await http.post(f"/repair-requests/{second}/void", json={"reason": "ок"})).status_code == 422
        assert (
            await http.post(f"/repair-requests/{second}/void", json={"reason": "сами устранили"})
        ).status_code == 200
        assert (await http.post(f"/repair-requests/{second}/void", json={"reason": "ещё раз"})).status_code == 409
    async with client(plant.tokens["service"]) as http:
        accept = {"status": "accepted", "executor_id": plant.users["service"]}
        assert (await http.post(f"/repair-requests/{first}/status", json=accept)).status_code == 200
        voided = await http.post(f"/repair-requests/{second}/status", json={"status": "accepted", **accept})
        assert voided.status_code == 409
        # a moment entered later may not precede the previous step or lie in the future
        early = {"status": "in_progress", "at": at(10).isoformat()}
        assert (await http.post(f"/repair-requests/{first}/status", json=early)).status_code == 422
    async with client(plant.tokens["master"]) as http:
        # once accepted, only the service can void it
        assert (await http.post(f"/repair-requests/{first}/void", json={"reason": "передумали"})).status_code == 403
        assert [r["id"] for r in (await http.get("/repair-requests", params={"open": "true"})).json()] == [first]
    async with client(plant.tokens["service"]) as http:
        assert (await http.post(f"/repair-requests/{first}/void", json={"reason": "дубль заявки"})).status_code == 200
        assert (await http.get("/repair-requests", params={"open": "true"})).json() == []
        rows = {r["id"]: r for r in (await http.get("/repair-requests")).json()}
        assert (rows[first]["void_reason"], rows[second]["void_reason"]) == ("дубль заявки", "сами устранили")


# ---------------------------------------------------------------- maintenance by running hours


async def test_maintenance_counts_run_hours_from_the_timeline(plant: Plant, session_factory: Factory) -> None:
    m1 = plant.m1.machine_id
    # 2 hours of RUN, then a stop up to now
    await build_timeline(session_factory, plant.m1, run_samples([(0, 120, "RUN"), (120, 179, "STOP")]))
    before = (T0 - timedelta(hours=1)).isoformat()
    async with client(plant.tokens["master"]) as http:
        plan = {"machine_id": m1, "name": "Смазка", "interval_run_hours": 2.5}
        assert (await http.post("/maintenance-plans", json=plan)).status_code == 403
    async with client(plant.tokens["service"]) as http:
        ids = {}
        for name, hours in (("Смазка направляющих", 2.5), ("Фильтр гидростанции", 2.1), ("Масло редуктора", 1.5)):
            body = {"machine_id": m1, "name": name, "interval_run_hours": hours, "counted_from": before}
            created = await http.post("/maintenance-plans", json=body)
            assert created.status_code == 201, created.text
            ids[name] = created.json()["id"]
        future = {
            "machine_id": m1,
            "name": "x",
            "interval_run_hours": 1,
            "counted_from": (NOW + timedelta(days=1)).isoformat(),
        }
        assert (await http.post("/maintenance-plans", json=future)).status_code == 422

    async with client(plant.tokens["master"]) as http:  # the customer sees the schedule
        plans = {p["name"]: p for p in (await http.get("/maintenance")).json()}
        assert {n: (p["run_hours"], p["status"]) for n, p in plans.items()} == {
            "Смазка направляющих": (2.0, "ok"),
            "Фильтр гидростанции": (2.0, "soon"),  # 95 % of the interval
            "Масло редуктора": (2.0, "due"),
        }
        lubrication = plans["Смазка направляющих"]
        assert lubrication["remaining_hours"] == 0.5 and lubrication["machine_name"] == "plant m1"
        # 2 hours of RUN over the ~3 hours the timeline exists: ~16 hours a day; 0.5 h remain = ~45 min
        now = datetime.now(UTC)
        rate = 2 / ((now - T0).total_seconds() / 86400)
        assert lubrication["run_hours_per_day"] == pytest.approx(rate, rel=0.01)
        forecast = datetime.fromisoformat(lubrication["forecast_at"])
        assert abs((forecast - (now + timedelta(days=0.5 / rate))).total_seconds()) < 60
        assert plans["Масло редуктора"]["forecast_at"] is None
        assert (await http.post(f"/maintenance-plans/{ids['Масло редуктора']}/records", json={})).status_code == 403

    oil = ids["Масло редуктора"]
    async with client(plant.tokens["service"]) as http:
        done = await http.post(f"/maintenance-plans/{oil}/records", json={"performed_at": at(60).isoformat()})
        assert done.status_code == 201
        record = done.json()["id"]
        oil_plan = next(p for p in (await http.get("/maintenance")).json() if p["id"] == oil)
        assert (oil_plan["run_hours"], oil_plan["status"]) == (1.0, "ok")  # counted again from the work
        assert datetime.fromisoformat(oil_plan["last_done_at"]) == at(60)
        assert (
            await http.post(f"/maintenance-records/{record}/void", json={"reason": "не тот станок"})
        ).status_code == 200
        assert (await http.post(f"/maintenance-records/{record}/void", json={"reason": "ещё раз"})).status_code == 409
        oil_plan = next(p for p in (await http.get("/maintenance")).json() if p["id"] == oil)
        assert (oil_plan["run_hours"], oil_plan["status"]) == (2.0, "due")
        # a plan switched off leaves the schedule; its records stay
        off = {"name": "Масло редуктора", "interval_run_hours": 1.5, "is_active": False}
        assert (await http.put(f"/maintenance-plans/{oil}", json=off)).status_code == 200
        assert oil not in {p["id"] for p in (await http.get("/maintenance")).json()}
        assert oil in {p["id"] for p in (await http.get("/maintenance", params={"inactive": "true"})).json()}

    async with client(plant.tokens["master"]) as http:
        (entry,) = (await http.get(f"/maintenance-plans/{oil}/records")).json()
        assert (entry["engineer"], entry["void_reason"]) == ("Петров П. П.", "не тот станок")
    async with client(plant.tokens["operator"]) as http:  # not for the operator
        assert (await http.get("/maintenance")).status_code == 403
