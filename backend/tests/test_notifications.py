"""Telegram notifications (ADR 0011) with the Bot API mocked: who gets what by role, nothing for another
organization, antispam, retries after errors, "restored", linking a chat, muted event types."""

from __future__ import annotations

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
from app.api.main import app
from app.core.config import Settings
from app.notify import store
from app.notify.service import MAX_ATTEMPTS, Notifier
from app.notify.telegram import TelegramApi
from app.repairs import maintenance, requests
from app.worker.__main__ import Worker
from tests.conftest import Seed, put_samples, seed_reference

Factory = async_sessionmaker[AsyncSession]
TOKEN = "123:TEST-TOKEN"
SETTINGS = Settings(telegram_bot_token=TOKEN, telegram_bot_username="mm_test_bot", public_url="https://mm.example.com")
PLAIN = Settings()  # a worker without Telegram, for building timelines
PASSWORD = "secret-password"
T0 = datetime.now(UTC).replace(microsecond=0)


class FakeTelegram:
    """The Bot API: records sendMessage, answers getUpdates; failures can be queued for sendMessage."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failures: list[httpx.Response | Exception] = []
        self.updates: list[dict[str, Any]] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        if method == "sendMessage":
            if self.failures:
                failure = self.failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            self.sent.append((int(body["chat_id"]), str(body["text"])))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if method == "getUpdates":
            result = [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]
            return httpx.Response(200, json={"ok": True, "result": result})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})


@dataclass
class Plant:
    a: Seed  # organization A: site a1, area of the master, machine m1, collector col-a1
    b: Seed  # organization B
    users: dict[str, int] = field(default_factory=dict)
    chats: dict[int, str] = field(default_factory=dict)  # chat id -> user key
    telegram: FakeTelegram = field(default_factory=FakeTelegram)
    notifier: Notifier | None = None

    def who_got(self) -> set[str]:
        return {self.chats[chat] for chat, _ in self.telegram.sent}

    def texts(self) -> list[str]:
        return [message for _, message in self.telegram.sent]

    def chat_of(self, key: str) -> int:
        return next(chat for chat, user in self.chats.items() if user == key)

    async def round(self, now: datetime) -> set[str]:
        """Watch the conditions and send what is due; who got a message in this round."""
        assert self.notifier is not None
        before = len(self.telegram.sent)
        await self.notifier.tick(now, force=True)
        await self.notifier.send_due(now)
        return {self.chats[chat] for chat, _ in self.telegram.sent[before:]}


@pytest.fixture
async def plant(session_factory: Factory) -> AsyncIterator[Plant]:
    a = await seed_reference(session_factory, org="acme", site="a1", machine="m1", collector="col-a1")
    b = await seed_reference(session_factory, org="bravo", site="b1", machine="m1", collector="col-b1")
    plant = Plant(a, b)
    roles = {
        "master_a": ("master", a.org_id, a.site_id, a.area_id),
        "engineer_a": ("engineer", a.org_id, a.site_id, None),
        "admin_a": ("admin", a.org_id, None, None),
        "operator_a": ("operator", a.org_id, a.site_id, a.area_id),
        "service": ("service", None, None, None),
        "admin_b": ("admin", b.org_id, None, None),
        "master_b": ("master", b.org_id, b.site_id, b.area_id),
    }
    async with session_factory() as session, session.begin():
        await identity.bootstrap(session, SETTINGS)
        for i, (key, (role, org, site, area)) in enumerate(roles.items()):
            user_id = await identity.create_user(session, key, PASSWORD, key.replace("_", " "))
            await identity.add_membership(session, user_id, role, org, site, area)
            await session.execute(
                text("INSERT INTO telegram_chats (user_id, chat_id) VALUES (:u, :c)"), {"u": user_id, "c": 1000 + i}
            )
            plant.users[key] = user_id
            plant.chats[1000 + i] = key
    api = TelegramApi(TOKEN, "https://telegram.test", plant.telegram.transport)
    plant.notifier = Notifier(SETTINGS, session_factory, api)
    app.state.settings = SETTINGS
    app.state.session_factory = session_factory
    app.state.redis = None
    yield plant
    await plant.notifier.aclose()


async def sample(factory: Factory, seed: Seed, at: datetime, **values: Any) -> None:
    await put_samples(factory, seed.machine_id, [{"ts": at, **values}], collector=seed.machine["collector_id"])


async def sql(factory: Factory, statement: str, **params: Any) -> None:
    async with factory() as session, session.begin():
        await session.execute(text(statement), params)


# ---------------------------------------------------------------- who gets what


async def test_alarm_goes_to_the_master_of_the_area_and_the_service(plant: Plant, session_factory: Factory) -> None:
    await sample(session_factory, plant.a, T0 - timedelta(seconds=5), alarm=True, alarm_code=1)
    assert await plant.round(T0) == {"master_a", "service"}
    message = plant.texts()[0]
    assert message.startswith("🔴 Авария — acme m1 (acme/a1 · a1 area)")
    assert f"https://mm.example.com/alarms?machine={plant.a.machine_id}&at=" in message

    await sample(session_factory, plant.b, T0, alarm=True, alarm_code=1)
    assert await plant.round(T0 + timedelta(seconds=10)) == {"master_b", "service"}


async def test_battery_and_fan_warnings_go_to_the_service(plant: Plant, session_factory: Factory) -> None:
    await sample(session_factory, plant.a, T0, alarm_code=2)
    await sample(session_factory, plant.b, T0, alarm_code=3)
    assert await plant.round(T0 + timedelta(seconds=1)) == {"service"}
    assert sorted(m.split(" — ")[0] for m in plant.texts()) == [
        "⚠️ Предупреждение: неисправность вентилятора",
        "⚠️ Предупреждение: низкий заряд батареи",
    ]


async def test_collector_offline_and_lost_buffer_go_to_the_service_and_the_admin(
    plant: Plant, session_factory: Factory
) -> None:
    await sql(
        session_factory, "UPDATE collectors SET last_heartbeat = :t WHERE id = 'col-a1'", t=T0 - timedelta(minutes=6)
    )
    await sql(
        session_factory, "UPDATE collectors SET last_heartbeat = :t WHERE id = 'col-b1'", t=T0 - timedelta(minutes=1)
    )
    assert await plant.round(T0) == {"service", "admin_a"}
    assert "не на связи больше 5 мин" in plant.texts()[0]

    await sql(
        session_factory, "UPDATE collectors SET last_heartbeat = :t WHERE id = 'col-a1'", t=T0 + timedelta(minutes=1)
    )
    assert await plant.round(T0 + timedelta(minutes=1, seconds=10)) == {"service", "admin_a"}
    assert plant.texts()[-1].startswith("✅ Восстановлено: коллектор «col-a1» снова на связи")

    await sql(
        session_factory, "UPDATE collectors SET buffer_lost_at = :t WHERE id = 'col-b1'", t=T0 + timedelta(minutes=2)
    )
    assert await plant.round(T0 + timedelta(minutes=2, seconds=10)) == {"service", "admin_b"}
    assert "пустым буфером" in plant.texts()[-1]
    assert await plant.round(T0 + timedelta(minutes=2, seconds=20)) == set()  # once


async def test_stop_without_a_reason_goes_to_the_master(plant: Plant, session_factory: Factory) -> None:
    worker = Worker(PLAIN, session_factory)
    samples = [{"ts": T0 - timedelta(minutes=15) + timedelta(seconds=s), "run": "STOP"} for s in range(0, 901, 10)]
    await put_samples(session_factory, plant.a.machine_id, samples, collector="col-a1")
    for _ in range(3):
        await worker.rebuild_timeline(plant.a.machine, T0)

    await sql(session_factory, "UPDATE areas SET stop_alert_minutes = 30 WHERE id = :a", a=plant.a.area_id)
    assert await plant.round(T0) == set()  # 13 minutes of STOP: below the setting of the area
    await sql(session_factory, "UPDATE areas SET stop_alert_minutes = 10 WHERE id = :a", a=plant.a.area_id)
    assert await plant.round(T0 + timedelta(seconds=10)) == {"master_a"}
    assert plant.texts()[-1] == (
        f"⏸ acme m1 (acme/a1 · a1 area): простой 10 мин без причины\nhttps://mm.example.com/operator/{plant.a.machine_id}"
    )

    async with session_factory() as session, session.begin():
        await journal.add_annotation(
            session, plant.a.machine_id, T0 - timedelta(minutes=15), None, plant.a.unplanned_reason_id, None, "master"
        )
    assert await plant.round(T0 + timedelta(seconds=20)) == {"master_a"}
    assert plant.texts()[-1].endswith("простой без причины закончился — указана причина: acme no material")


async def test_repair_requests_go_to_the_service_then_to_the_client(plant: Plant, session_factory: Factory) -> None:
    async with session_factory() as session, session.begin():
        old = await requests.create(session, plant.a.machine_id, "old", T0 - timedelta(days=1), None, None, "x")
    assert await plant.round(T0) == set()  # the first start: the history so far is not news

    async with session_factory() as session, session.begin():
        new = await requests.create(session, plant.a.machine_id, "Течь масла", T0, None, None, "acme master")
    assert await plant.round(T0 + timedelta(seconds=10)) == {"service"}
    assert plant.texts()[-1].startswith(f"🛠 Новая заявка №{new} — acme m1")
    assert old != new

    async with session_factory() as session, session.begin():
        request = await requests.get(session, new)
        assert request is not None
        executor = {"id": plant.users["service"], "name": "Сервисный инженер"}
        now = datetime.now(UTC)  # after the creation, which the database stamps
        await requests.advance(session, request, "accepted", now, now, executor, None, None, None, "service")
    assert await plant.round(T0 + timedelta(seconds=20)) == {"admin_a", "master_a"}
    assert plant.texts()[-1].startswith(f"🛠 Заявка №{new} принята, исполнитель: Сервисный инженер — acme m1")


async def test_maintenance_due_goes_to_the_service(plant: Plant, session_factory: Factory) -> None:
    worker = Worker(PLAIN, session_factory)
    start = T0 - timedelta(hours=2)
    samples = [{"ts": start + timedelta(seconds=s), "run": "START", "motion": True} for s in range(0, 7200, 10)]
    await put_samples(session_factory, plant.a.machine_id, samples, collector="col-a1")
    for _ in range(4):
        await worker.rebuild_timeline(plant.a.machine, T0)
    async with session_factory() as session, session.begin():
        await maintenance.create_plan(session, plant.a.machine_id, "Замена масла", 2, start, None, None)
    assert await plant.round(T0) == {"service"}
    assert plant.texts()[-1].startswith("🗓 Подходит ТО «Замена масла» — acme m1")
    assert await plant.round(T0 + timedelta(minutes=10)) == set()  # once until the work is recorded


async def test_another_organization_gets_nothing(plant: Plant, session_factory: Factory) -> None:
    """Every event about organization A: nobody of B is told, nobody of A is told what is not theirs."""
    await sample(session_factory, plant.a, T0, alarm=True, alarm_code=1)
    await sql(session_factory, "UPDATE collectors SET last_heartbeat = :t, buffer_lost_at = :t WHERE id = 'col-a1'",
              t=T0 - timedelta(minutes=7))  # fmt: skip
    await plant.round(T0)
    async with session_factory() as session, session.begin():
        new = await requests.create(session, plant.a.machine_id, "Стук", T0, None, None, "acme master")
        request = await requests.get(session, new)
        assert request is not None
        executor = {"id": plant.users["service"], "name": "Сервис"}
        now = datetime.now(UTC)  # after the creation, which the database stamps
        await requests.advance(session, request, "accepted", now, now, executor, None, None, None, "service")
    await plant.round(T0 + timedelta(seconds=10))
    assert plant.who_got() == {"master_a", "service", "admin_a"}
    assert not plant.who_got() & {"admin_b", "master_b", "engineer_a", "operator_a"}


# ---------------------------------------------------------------- antispam, restored, retries


async def test_antispam_and_restored(plant: Plant, session_factory: Factory) -> None:
    async def alarm(at: datetime, code: int) -> set[str]:
        await sample(session_factory, plant.a, at - timedelta(seconds=1), alarm=code == 1, alarm_code=code)
        return await plant.round(at)

    assert await alarm(T0, 1) == {"master_a", "service"}
    assert await alarm(T0 + timedelta(seconds=30), 1) == set()  # still the same alarm
    assert await alarm(T0 + timedelta(minutes=1), 0) == {"master_a", "service"}
    assert plant.texts()[-1] == "✅ Восстановлено: авария снята — acme m1 (acme/a1 · a1 area)"
    assert await alarm(T0 + timedelta(minutes=2), 1) == set()  # again within 15 minutes: not announced
    assert await alarm(T0 + timedelta(minutes=3), 0) == set()  # and so its end is not announced either
    assert await alarm(T0 + timedelta(minutes=16), 1) == {"master_a", "service"}
    # late data is not news: a sample older than 10 minutes neither starts nor ends anything
    assert await alarm(T0 + timedelta(minutes=40), 0) == {"master_a", "service"}
    await sample(session_factory, plant.a, T0 + timedelta(minutes=41), alarm=True, alarm_code=1)
    assert await plant.round(T0 + timedelta(minutes=60)) == set()


async def test_failed_sends_are_retried(plant: Plant, session_factory: Factory) -> None:
    notifier = plant.notifier
    assert notifier is not None
    telegram = plant.telegram
    await sample(session_factory, plant.a, T0, alarm=True, alarm_code=1)
    telegram.failures = [
        httpx.Response(500, json={"ok": False, "description": "Internal Server Error"}),
        httpx.ConnectError("network is unreachable"),
    ]
    await notifier.tick(T0, force=True)
    assert await notifier.send_due(T0) == 0  # both messages failed: 500 and a network error
    assert await notifier.send_due(T0 + timedelta(seconds=5)) == 0  # not due yet
    assert await notifier.send_due(T0 + timedelta(seconds=11)) == 2  # retried after 10 s
    assert plant.who_got() == {"master_a", "service"}
    assert len(telegram.sent) == 2  # each delivered once

    # Telegram asks to wait: the retry honours retry_after
    await sql(
        session_factory, "UPDATE collectors SET last_heartbeat = :t WHERE id = 'col-a1'", t=T0 - timedelta(minutes=6)
    )
    await notifier.tick(T0, force=True)
    telegram.failures = [
        httpx.Response(429, json={"ok": False, "description": "Too Many", "parameters": {"retry_after": 40}})
    ]
    assert await notifier.send_due(T0 + timedelta(minutes=1)) == 1
    assert await notifier.send_due(T0 + timedelta(minutes=1, seconds=30)) == 0
    assert await notifier.send_due(T0 + timedelta(minutes=1, seconds=41)) == 1

    # the user blocked the bot: dropped at once, the chat is unlinked
    await sample(session_factory, plant.b, T0, alarm=True, alarm_code=1)
    await notifier.tick(T0 + timedelta(minutes=2), force=True)
    telegram.failures = [
        httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"})
    ]
    await notifier.send_due(T0 + timedelta(minutes=2))
    async with session_factory() as session:
        chats = {r.user_id for r in await session.execute(text("SELECT user_id FROM telegram_chats"))}
    assert len(chats) == len(plant.users) - 1

    # and a message that never gets through is dropped after the last attempt
    async with session_factory() as session, session.begin():
        await store.enqueue(session, [plant.users["admin_a"]], "buffer_lost", "collector:x", "test", T0)
    telegram.failures = [httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})] * MAX_ATTEMPTS
    at = T0 + timedelta(minutes=5)
    for _ in range(MAX_ATTEMPTS):
        await notifier.send_due(at)
        at += timedelta(hours=1)
    async with session_factory() as session:
        row = (
            await session.execute(text("SELECT attempts, failed_at FROM notification_outbox WHERE text = 'test'"))
        ).one()
    assert row.attempts == MAX_ATTEMPTS and row.failed_at is not None


async def test_telegram_errors_do_not_stop_the_worker(plant: Plant, session_factory: Factory) -> None:
    notifier = plant.notifier
    assert notifier is not None
    worker = Worker(SETTINGS, session_factory, notifier=notifier)
    plant.telegram.failures = [httpx.ConnectError("down")] * 10
    await sample(session_factory, plant.a, T0, alarm=True, alarm_code=1)
    await worker.tick(T0)  # timeline, OEE and the notifier: nothing raises
    assert await notifier.send_due(T0) == 0

    async def broken(now: datetime, force: bool = False) -> None:
        raise RuntimeError("bug")

    notifier.tick = broken  # type: ignore[method-assign]
    await worker.tick(T0 + timedelta(seconds=5))  # logged, the worker goes on


# ---------------------------------------------------------------- linking and the profile


@contextlib.asynccontextmanager
async def client(token: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1", headers=headers) as http:
        yield http


async def login(factory: Factory, key: str) -> str:
    async with factory() as session:
        token = await identity.login(session, SETTINGS, key, PASSWORD)
    assert token is not None
    return token


def update(update_id: int, chat_id: int, message: str, chat_type: str = "private") -> dict[str, Any]:
    chat = {"id": chat_id, "type": chat_type, "username": "ivan"}
    return {"update_id": update_id, "message": {"message_id": update_id, "chat": chat, "text": message}}


async def test_linking_a_chat(plant: Plant, session_factory: Factory) -> None:
    notifier = plant.notifier
    assert notifier is not None
    token = await login(session_factory, "master_a")
    await sql(session_factory, "DELETE FROM telegram_chats WHERE user_id = :u", u=plant.users["master_a"])
    async with client(token) as http:
        assert (await http.get("/profile/telegram")).json()["linked"] is False
        link = (await http.post("/profile/telegram/link")).json()
    assert link["url"].startswith("https://t.me/mm_test_bot?start=")
    code = link["url"].split("start=")[1]

    plant.telegram.updates = [
        update(1, 555, f"/start {code}"),
        update(2, 556, f"/start {code}"),  # the same code again: spent
        update(3, 777, f"/start {code}", chat_type="group"),  # groups are ignored
        update(4, 558, "привет"),
    ]
    assert await notifier.poll_once(timeout_s=0) == 4
    replies = dict(plant.telegram.sent)
    assert replies[555].startswith("Готово: сюда будут приходить уведомления системы мониторинга для master a")
    assert replies[556].startswith("Ссылка недействительна или устарела")
    assert 777 not in replies
    assert replies[558].startswith("Это бот уведомлений")
    assert await notifier.poll_once(timeout_s=0) == 0  # the position is kept
    async with client(token) as http:
        status = (await http.get("/profile/telegram")).json()
    assert (status["linked"], status["username"], status["bot"]) == (True, "ivan", "mm_test_bot")

    # an expired code does not link
    async with session_factory() as session, session.begin():
        old_code, _ = await store.new_link_code(session, plant.users["engineer_a"], T0 - timedelta(minutes=20))
    plant.telegram.updates = [update(5, 600, f"/start {old_code}")]
    await notifier.poll_once(timeout_s=0)
    assert dict(plant.telegram.sent)[600].startswith("Ссылка недействительна")

    # unlinking: in the profile, or /stop in the chat
    async with client(token) as http:
        assert (await http.delete("/profile/telegram")).json()["linked"] is False
    async with session_factory() as session:
        assert await store.chat_of(session, plant.users["master_a"]) is None
    plant.telegram.updates = [update(6, plant.chat_of("service"), "/stop")]
    await notifier.poll_once(timeout_s=0)
    async with session_factory() as session:
        assert await store.chat_of(session, plant.users["service"]) is None


async def test_event_types_of_the_profile(plant: Plant, session_factory: Factory) -> None:
    master = await login(session_factory, "master_a")
    async with client(master) as http:
        types = (await http.get("/profile/notifications")).json()
        assert types == [{"type": t, "enabled": True} for t in ("alarm", "stop_no_reason", "repair_status")]
        changed = await http.put("/profile/notifications", json={"enabled": {"alarm": False}})
        assert changed.json()[0] == {"type": "alarm", "enabled": False}
        assert (await http.put("/profile/notifications", json={"enabled": {"warning": False}})).status_code == 422

    await sample(session_factory, plant.a, T0, alarm=True, alarm_code=1)
    assert await plant.round(T0) == {"service"}  # the master switched alarms off

    service = await login(session_factory, "service")
    async with client(service) as http:
        names = [t["type"] for t in (await http.get("/profile/notifications")).json()]
    assert names == ["alarm", "warning", "collector_offline", "buffer_lost", "repair_new", "maintenance_due", "backup"]
    operator = await login(session_factory, "operator_a")
    async with client(operator) as http:
        assert (await http.get("/profile/notifications")).json() == []

    async with session_factory() as session, session.begin():
        _, kiosk = await identity.issue_device_token(session, "kiosk", plant.a.machine_id, None, None)
    async with client(kiosk) as http:
        assert (await http.get("/profile/telegram")).status_code == 403  # devices have no profile


# ---------------------------------------------------------------- backups


async def backup_run(factory: Factory, kind: str, ok: bool, at: datetime, message: str = "") -> None:
    """What deploy/backup/mm-backup.sh writes after a run."""
    await sql(
        factory,
        "INSERT INTO backup_runs (kind, ok, finished_at, message) VALUES (:k, :ok, :at, :m)",
        k=kind, ok=ok, at=at, m=message,
    )  # fmt: skip


async def test_backups_go_to_the_service_and_the_superadmin(plant: Plant, session_factory: Factory) -> None:
    async with session_factory() as session:
        superadmin = await identity.login(session, SETTINGS, SETTINGS.admin_username, SETTINGS.admin_password)
    assert superadmin is not None
    async with session_factory() as session, session.begin():
        user_id = (
            await session.execute(text("SELECT id FROM users WHERE login = :l"), {"l": SETTINGS.admin_username})
        ).scalar_one()
        await session.execute(text("INSERT INTO telegram_chats (user_id, chat_id) VALUES (:u, 2000)"), {"u": user_id})
    plant.chats[2000] = "superadmin"
    async with client(superadmin) as http:  # the superadmin can switch it off like any other type
        assert [t["type"] for t in (await http.get("/profile/notifications")).json()] == ["backup"]

    # the backup container never reported (development): nothing is said, however long
    assert await plant.round(T0) == set()

    # a nightly backup failed: the service and the superadmin, nobody of the customers
    await backup_run(session_factory, "backup", True, T0 - timedelta(hours=25))
    await backup_run(session_factory, "backup", False, T0 - timedelta(minutes=5), "upload to s3:mm failed")
    assert await plant.round(T0) == {"service", "superadmin"}
    assert "Ночной бэкап базы завершился ошибкой\nupload to s3:mm failed" in plant.texts()[-1]
    assert await plant.round(T0 + timedelta(minutes=1)) == set()  # the same failure is not repeated

    # still no good backup 26 hours after the last one: one more message, about the age
    later = T0 + timedelta(hours=1, minutes=10)
    assert await plant.round(later) == {"service", "superadmin"}
    assert "Нет успешного бэкапа базы больше 26 ч" in plant.texts()[-1]

    # the next night succeeds: both conditions are restored
    await backup_run(session_factory, "backup", True, later + timedelta(minutes=1))
    before = len(plant.texts())
    assert await plant.round(later + timedelta(minutes=2)) == {"service", "superadmin"}
    restored = plant.texts()[before:]
    assert sum("ночной бэкап базы выполнен" in t for t in restored) == 2  # one per recipient
    assert sum("свежий бэкап базы есть" in t for t in restored) == 2

    # a restore check that fails, then passes
    await backup_run(session_factory, "restore_check", False, later + timedelta(minutes=3), "pg_restore failed")
    assert await plant.round(later + timedelta(minutes=4)) == {"service", "superadmin"}
    assert "restore-check" in plant.texts()[-1]
    await backup_run(session_factory, "restore_check", True, later + timedelta(minutes=30))
    assert await plant.round(later + timedelta(minutes=31)) == {"service", "superadmin"}
    assert "проверка восстановления бэкапа прошла" in plant.texts()[-1]

    # switched off in the profile: the superadmin is not told any more
    async with client(superadmin) as http:
        assert (await http.put("/profile/notifications", json={"enabled": {"backup": False}})).status_code == 200
    await backup_run(session_factory, "backup", False, later + timedelta(hours=3), "pg_dump failed")
    assert await plant.round(later + timedelta(hours=3, minutes=1)) == {"service"}
