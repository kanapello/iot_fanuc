"""The notifier of the worker (ADR 0011).

``tick`` watches conditions and puts messages into the outbox for the users whose role and scope cover
the subject (the scope is built exactly as for their requests to the API). ``run_sender`` sends the
outbox to Telegram with retries, ``run_polling`` answers the bot (linking a chat by /start <code>).
Errors of Telegram are logged and never stop the worker.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import identity, journal
from app.admin import service as admin
from app.core.access import MACHINE_VIEW, Scope
from app.core.config import Settings
from app.notify import events, store
from app.notify.telegram import LONG_POLL_S, TelegramApi, TelegramError
from app.repairs import maintenance, requests
from app.reports import alarms
from app.timeline import service as timeline

log = logging.getLogger("app.notify")

FRESH = timedelta(minutes=10)  # older telemetry neither starts nor ends an alarm: late data is not news
COLLECTOR_GONE = timedelta(days=1)  # silent longer than this: decommissioned, not news either
BUFFER_LOST_NEWS = timedelta(hours=1)
MAINTENANCE_EVERY = timedelta(minutes=5)
RETRY_S = (10, 30, 60, 300, 900, 1800)
MAX_ATTEMPTS = 8
HISTORY_CURSOR = "repair_history"
UPDATES_CURSOR = "telegram_updates"
DEFAULT_STOP_ALERT_MINUTES = 10

LINKED = "Готово: сюда будут приходить уведомления системы мониторинга для {name}. Типы событий — в профиле."
LINK_INVALID = "Ссылка недействительна или устарела. Получите новую в профиле: «Подключить Telegram»."
UNLINKED = "Уведомления отключены. Подключить снова — в профиле: «Подключить Telegram»."
HELP = "Это бот уведомлений системы мониторинга станков. Подключение — в профиле: «Подключить Telegram»."

SessionFactory = async_sessionmaker[AsyncSession]


class Recipients:
    """Who may be told what. Built from the scopes of the linked users, the same as in the API: the
    subject must be in the user's scope and a role of the event type must be granted on its node."""

    def __init__(self, users: Sequence[tuple[Scope, set[str]]]) -> None:
        self._users = users

    def for_machine(self, event_type: str, machine_id: int) -> list[int]:
        roles = events.EVENT_TYPES[event_type].roles
        return [
            scope.user_id
            for scope, muted in self._users
            if scope.user_id is not None
            and event_type not in muted
            and scope.can_machine(MACHINE_VIEW, machine_id)
            and scope.roles_on_machine(machine_id) & roles
        ]

    def for_site(self, event_type: str, site_id: int) -> list[int]:
        roles = events.EVENT_TYPES[event_type].roles
        return [
            scope.user_id
            for scope, muted in self._users
            if scope.user_id is not None
            and event_type not in muted
            and site_id in scope.visible_site_ids
            and scope.roles_on_site(site_id) & roles
        ]


class Notifier:
    def __init__(self, settings: Settings, session_factory: SessionFactory, api: TelegramApi | None = None) -> None:
        self._settings = settings
        self._factory = session_factory
        self._api = api or TelegramApi(settings.telegram_bot_token or "", settings.telegram_api_url)
        self._repeat = timedelta(minutes=settings.notify_repeat_minutes)
        self._checked_at: datetime | None = None
        self._maintenance_at: datetime | None = None

    async def aclose(self) -> None:
        await self._api.aclose()

    # ---------------------------------------------------------------- watching

    async def tick(self, now: datetime, force: bool = False) -> None:
        """Look at every watched condition; at most once in ``notify_interval_s`` unless forced."""
        recent = (
            self._checked_at is not None and (now - self._checked_at).total_seconds() < self._settings.notify_interval_s
        )
        if recent and not force:
            return
        self._checked_at = now
        async with self._factory() as session:
            recipients = await self._recipients(session)
            machines = await admin.list_machines_system(session, active_only=False)
        active = [m for m in machines if m["is_active"]]
        check_maintenance = force or self._maintenance_at is None or now - self._maintenance_at >= MAINTENANCE_EVERY
        steps: list[tuple[str, Callable[[AsyncSession], Any]]] = [
            ("machines", lambda s: self._machines(s, active, recipients, now)),
            ("collectors", lambda s: self._collectors(s, recipients, now)),
            ("repairs", lambda s: self._repairs(s, {m["id"]: m for m in machines}, recipients, now)),
        ]
        if check_maintenance:
            self._maintenance_at = now
            steps.append(("maintenance", lambda s: self._maintenance(s, active, recipients, now)))
        for name, step in steps:
            try:
                async with self._factory() as session, session.begin():
                    await step(session)
            except Exception:
                log.exception("notifications: checking %s failed", name)

    async def _recipients(self, session: AsyncSession) -> Recipients:
        users = []
        for user in await store.linked_users(session):
            scope = await identity.user_scope(session, user["id"])
            if scope is not None:
                users.append((scope, user["muted"]))
        return Recipients(users)

    async def _watch(
        self,
        session: AsyncSession,
        event_type: str,
        subject: str,
        holds: bool,
        now: datetime,
        who: Callable[[], list[int]],
        began: Callable[[], str],
        ended: Callable[[], str] | None = None,
    ) -> None:
        if holds:
            if await store.condition_began(session, event_type, subject, now, self._repeat):
                await store.enqueue(session, who(), event_type, subject, began(), now)
        elif await store.condition_ended(session, event_type, subject, now) and ended is not None:
            await store.enqueue(session, who(), event_type, subject, ended(), now)

    async def _machines(
        self, session: AsyncSession, machines: list[dict[str, Any]], recipients: Recipients, now: datetime
    ) -> None:
        base = self._settings.public_url
        stop_minutes = await admin.area_stop_alert_minutes(session)
        for machine in machines:
            machine_id = machine["id"]
            subject = f"machine:{machine_id}"
            sample = await timeline.latest_sample(session, machine_id)
            if sample is not None and sample["connected"] and now - sample["ts"] <= FRESH:
                code = sample.get("alarm_code")
                if code == 1:
                    if await store.condition_began(session, "alarm", subject, now, self._repeat):
                        # the code and the text of the alarm, if the collector reads the alarm list
                        found = await alarms.episodes(session, [machine_id], now - timedelta(hours=1), now, now)
                        episode = next((e for e in found if e["active"]), None)
                        message = events.alarm(machine, episode, episode["start_ts"] if episode else now, base)
                        await store.enqueue(
                            session, recipients.for_machine("alarm", machine_id), "alarm", subject, message, now
                        )
                elif await store.condition_ended(session, "alarm", subject, now):
                    message = events.restored("alarm", machine)
                    await store.enqueue(
                        session, recipients.for_machine("alarm", machine_id), "alarm", subject, message, now
                    )
                await self._watch(
                    session, "warning", subject, code in (2, 3), now,
                    partial(recipients.for_machine, "warning", machine_id),
                    partial(events.warning, machine, int(code or 0), base),
                    partial(events.restored, "warning", machine),
                )  # fmt: skip

            state = await timeline.current_state(session, machine_id)
            minutes = stop_minutes.get(machine["area_id"] or -1, DEFAULT_STOP_ALERT_MINUTES)
            reason = None
            if state is not None and state["state"] == "STOP":
                reason = await journal.current_reason(
                    session, machine_id, state["downtime_since"] or state["since"], now
                )
            holds = (
                state is not None
                and state["state"] == "STOP"
                and now - state["since"] >= timedelta(minutes=minutes)
                and reason is None
            )
            now_state = events.STATES.get((state or {}).get("state", "OFFLINE"), "")
            detail = f"указана причина: {reason['name']}" if reason else f"сейчас: {now_state}"
            await self._watch(
                session, "stop_no_reason", subject, holds, now,
                partial(recipients.for_machine, "stop_no_reason", machine_id),
                partial(events.stop_no_reason, machine, minutes, base),
                partial(events.restored, "stop_no_reason", machine, detail),
            )  # fmt: skip

    async def _collectors(self, session: AsyncSession, recipients: Recipients, now: datetime) -> None:
        minutes = self._settings.collector_offline_notify_minutes
        for collector in await admin.list_collectors_system(session):
            subject = f"collector:{collector['id']}"
            heartbeat = collector["last_heartbeat"]
            if heartbeat is not None and now - heartbeat <= COLLECTOR_GONE:
                await self._watch(
                    session, "collector_offline", subject, now - heartbeat > timedelta(minutes=minutes), now,
                    partial(recipients.for_site, "collector_offline", collector["site_id"]),
                    partial(events.collector_offline, collector, minutes),
                    partial(events.restored, "collector_offline", collector),
                )  # fmt: skip
            lost = collector["buffer_lost_at"]
            fresh_loss = lost is not None and now - lost <= BUFFER_LOST_NEWS
            if fresh_loss and await store.occurred(session, "buffer_lost", subject, lost, now, self._repeat):
                who = recipients.for_site("buffer_lost", collector["site_id"])
                await store.enqueue(session, who, "buffer_lost", subject, events.buffer_lost(collector), now)

    async def _repairs(
        self, session: AsyncSession, machines: dict[int, dict[str, Any]], recipients: Recipients, now: datetime
    ) -> None:
        position = await store.cursor(session, HISTORY_CURSOR)
        if position is None:  # the first start: the history so far is not news
            await store.set_cursor(session, HISTORY_CURSOR, await requests.last_history_id(session))
            return
        base = self._settings.public_url
        for step in await requests.history_since(session, position):
            position = step["id"]
            machine = machines.get(step["machine_id"])
            if machine is None:
                continue
            if step["action"] == "created":
                event_type, subject, message = (
                    "repair_new",
                    f"request:{step['request_id']}",
                    events.repair_new(step, machine, base),
                )
            elif step["action"] in events.STATUSES:
                event_type = "repair_status"
                subject = f"request:{step['request_id']}:{step['action']}"
                message = events.repair_status(step, machine, base)
            else:
                continue  # a new executor is not a change of the status
            if await store.occurred(session, event_type, subject, step["ts"], now, self._repeat):
                await store.enqueue(
                    session, recipients.for_machine(event_type, machine["id"]), event_type, subject, message, now
                )
        await store.set_cursor(session, HISTORY_CURSOR, position)

    async def _maintenance(
        self, session: AsyncSession, machines: list[dict[str, Any]], recipients: Recipients, now: datetime
    ) -> None:
        by_id = {m["id"]: m for m in machines}
        base = self._settings.public_url
        for plan in await maintenance.schedule(session, list(by_id), now):
            machine = by_id[plan["machine_id"]]
            await self._watch(
                session, "maintenance_due", f"plan:{plan['id']}", plan["status"] in ("soon", "due"), now,
                partial(recipients.for_machine, "maintenance_due", machine["id"]),
                partial(events.maintenance_due, plan, machine, base),
            )  # fmt: skip

    # ---------------------------------------------------------------- sending

    async def send_due(self, now: datetime) -> int:
        """Send what is due; a failure is retried later. Returns the number of messages sent."""
        sent = 0
        async with self._factory() as session, session.begin():
            for message in await store.due_messages(session, now):
                if message["chat_id"] is None:
                    await store.mark_failed(session, message["id"], now, "the user has no Telegram chat any more")
                    continue
                try:
                    await self._api.send_message(message["chat_id"], message["text"])
                except TelegramError as exc:
                    await self._send_failed(session, message, exc, now)
                else:
                    await store.mark_sent(session, message["id"], now)
                    sent += 1
        return sent

    async def _send_failed(
        self, session: AsyncSession, message: dict[str, Any], exc: TelegramError, now: datetime
    ) -> None:
        if exc.status == 403 or (exc.status == 400 and "chat not found" in exc.description.lower()):
            # the user blocked the bot or deleted the chat: nothing will ever reach it
            await store.mark_failed(session, message["id"], now, str(exc))
            await store.unlink_chat(session, message["chat_id"])
            log.warning("telegram chat unlinked: %s", exc, extra={"user_id": message["user_id"]})
            return
        attempt = message["attempts"] + 1
        if attempt >= MAX_ATTEMPTS:
            await store.mark_failed(session, message["id"], now, str(exc))
            log.error(
                "telegram message dropped after %s attempts: %s", attempt, exc, extra={"user_id": message["user_id"]}
            )
            return
        delay = exc.retry_after or RETRY_S[min(attempt, len(RETRY_S)) - 1]
        await store.mark_retry(session, message["id"], now + timedelta(seconds=delay), str(exc))
        log.warning("telegram send failed, retry in %s s: %s", delay, exc, extra={"user_id": message["user_id"]})

    async def run_sender(self, interval_s: float = 2.0) -> None:
        while True:
            try:
                await self.send_due(datetime.now(UTC))
            except Exception:
                log.exception("sending notifications failed")
            await asyncio.sleep(interval_s)

    # ---------------------------------------------------------------- the bot

    async def poll_once(self, timeout_s: int = LONG_POLL_S) -> int:
        """One long poll of getUpdates; each update is answered and the position saved."""
        async with self._factory() as session:
            offset = await store.cursor(session, UPDATES_CURSOR) or 0
        updates = await self._api.get_updates(offset, timeout_s)
        for update in updates:
            await self._answer(update)
            async with self._factory() as session, session.begin():
                await store.set_cursor(session, UPDATES_CURSOR, int(update["update_id"]) + 1)
        return len(updates)

    async def _answer(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        if not chat or not text or chat.get("type") != "private":
            return  # only private chats: notifications are personal
        chat_id = int(chat["id"])
        now = datetime.now(UTC)
        command, _, argument = text.partition(" ")
        if command == "/start" and argument.strip():
            async with self._factory() as session, session.begin():
                user = await store.redeem_link_code(session, argument.strip(), chat_id, chat.get("username"), now)
            reply = LINKED.format(name=user["full_name"] or user["login"]) if user else LINK_INVALID
        elif command == "/stop":
            async with self._factory() as session, session.begin():
                await store.unlink_chat(session, chat_id)
            reply = UNLINKED
        else:
            reply = HELP
        try:
            await self._api.send_message(chat_id, reply)
        except TelegramError as exc:
            log.warning("telegram reply failed: %s", exc)

    async def run_polling(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.poll_once()
                delay = 1.0
            except TelegramError as exc:
                log.warning("telegram getUpdates failed: %s", exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            except Exception:
                log.exception("telegram polling failed")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
