"""Tables of the notifications: chats and link codes, muted types, watched conditions, the outbox, cursors."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LINK_CODE_TTL = timedelta(minutes=15)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ---------------------------------------------------------------- chats


async def new_link_code(session: AsyncSession, user_id: int, now: datetime) -> tuple[str, datetime]:
    """A one-time code for t.me/<bot>?start=<code>; the previous unused codes of the user stop working."""
    code = secrets.token_urlsafe(24)  # letters, digits, - and _: what Telegram allows in a start parameter
    expires = now + LINK_CODE_TTL
    await session.execute(
        text("DELETE FROM telegram_link_codes WHERE user_id = :u AND used_at IS NULL"), {"u": user_id}
    )
    await session.execute(
        text("INSERT INTO telegram_link_codes (code_hash, user_id, expires_at) VALUES (:h, :u, :e)"),
        {"h": _hash(code), "u": user_id, "e": expires},
    )
    return code, expires


async def redeem_link_code(
    session: AsyncSession, code: str, chat_id: int, username: str | None, now: datetime
) -> dict[str, Any] | None:
    """Link the chat to the user of a valid code; the code is spent. None if it is unknown, used or expired.

    One user - one chat: a new chat replaces the old one; a chat moves away from another user."""
    row = (
        await session.execute(
            text(
                "UPDATE telegram_link_codes SET used_at = :now "
                "WHERE code_hash = :h AND used_at IS NULL AND expires_at > :now RETURNING user_id"
            ),
            {"h": _hash(code), "now": now},
        )
    ).first()
    if row is None:
        return None
    await session.execute(
        text("DELETE FROM telegram_chats WHERE chat_id = :c AND user_id <> :u"), {"c": chat_id, "u": row.user_id}
    )
    await session.execute(
        text(
            "INSERT INTO telegram_chats (user_id, chat_id, username, linked_at) VALUES (:u, :c, :n, :now) "
            "ON CONFLICT (user_id) DO UPDATE SET chat_id = :c, username = :n, linked_at = :now"
        ),
        {"u": row.user_id, "c": chat_id, "n": username, "now": now},
    )
    user = (
        await session.execute(text("SELECT id, login, full_name FROM users WHERE id = :u"), {"u": row.user_id})
    ).first()
    return dict(user._mapping) if user else None


async def chat_of(session: AsyncSession, user_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT chat_id, username, linked_at FROM telegram_chats WHERE user_id = :u"), {"u": user_id}
        )
    ).first()
    return dict(row._mapping) if row else None


async def unlink(session: AsyncSession, user_id: int) -> None:
    await session.execute(text("DELETE FROM telegram_chats WHERE user_id = :u"), {"u": user_id})


async def unlink_chat(session: AsyncSession, chat_id: int) -> None:
    await session.execute(text("DELETE FROM telegram_chats WHERE chat_id = :c"), {"c": chat_id})


async def linked_users(session: AsyncSession) -> list[dict[str, Any]]:
    """Active users with a chat, and the event types each of them switched off."""
    rows = await session.execute(
        text(
            "SELECT u.id, COALESCE(array_agg(m.event_type) FILTER (WHERE m.event_type IS NOT NULL), '{}') AS muted "
            "FROM telegram_chats c JOIN users u ON u.id = c.user_id AND u.is_active "
            "LEFT JOIN notification_mutes m ON m.user_id = u.id GROUP BY u.id ORDER BY u.id"
        )
    )
    return [{"id": r.id, "muted": set(r.muted)} for r in rows]


# ---------------------------------------------------------------- muted types


async def muted_types(session: AsyncSession, user_id: int) -> set[str]:
    rows = await session.execute(text("SELECT event_type FROM notification_mutes WHERE user_id = :u"), {"u": user_id})
    return {r.event_type for r in rows}


async def set_muted(session: AsyncSession, user_id: int, event_types: Collection[str], muted: Collection[str]) -> None:
    """``muted`` among ``event_types`` are switched off, the rest of ``event_types`` on."""
    await session.execute(
        text("DELETE FROM notification_mutes WHERE user_id = :u AND event_type = ANY(:t)"),
        {"u": user_id, "t": list(event_types)},
    )
    for event_type in muted:
        await session.execute(
            text("INSERT INTO notification_mutes (user_id, event_type) VALUES (:u, :t) ON CONFLICT DO NOTHING"),
            {"u": user_id, "t": event_type},
        )


# ---------------------------------------------------------------- conditions and antispam


async def _condition(session: AsyncSession, event_type: str, subject: str) -> Any:
    return (
        await session.execute(
            text(
                "SELECT active, since, announced, announced_at FROM notification_conditions "
                "WHERE event_type = :t AND subject = :s FOR UPDATE"
            ),
            {"t": event_type, "s": subject},
        )
    ).first()


async def _save(
    session: AsyncSession,
    event_type: str,
    subject: str,
    active: bool,
    since: datetime,
    announced: bool,
    announced_at: datetime | None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO notification_conditions (event_type, subject, active, since, announced, announced_at) "
            "VALUES (:t, :s, :a, :since, :n, :at) ON CONFLICT (event_type, subject) DO UPDATE SET "
            "active = :a, since = :since, announced = :n, announced_at = :at"
        ),
        {"t": event_type, "s": subject, "a": active, "since": since, "n": announced, "at": announced_at},
    )


async def condition_began(
    session: AsyncSession, event_type: str, subject: str, now: datetime, repeat: timedelta
) -> bool:
    """The condition holds now. True when this is a new episode that may be announced: not announced
    within ``repeat`` (antispam); an episode that is not announced has no "restored" either."""
    row = await _condition(session, event_type, subject)
    if row is not None and row.active:
        return False
    announce = row is None or row.announced_at is None or now - row.announced_at >= repeat
    await _save(session, event_type, subject, True, now, announce, now if announce else row.announced_at)
    return announce


async def condition_ended(session: AsyncSession, event_type: str, subject: str, now: datetime) -> bool:
    """The condition no longer holds. True when its beginning was announced: then its end is announced too."""
    row = await _condition(session, event_type, subject)
    if row is None or not row.active:
        return False
    await _save(session, event_type, subject, False, now, False, row.announced_at)
    return bool(row.announced)


async def occurred(
    session: AsyncSession, event_type: str, subject: str, at: datetime, now: datetime, repeat: timedelta
) -> bool:
    """A one-time event at ``at`` (a lost buffer, a step of a request). True when it is new and may be
    announced (antispam)."""
    row = await _condition(session, event_type, subject)
    if row is not None and row.since >= at:
        return False  # this one, or a later one, is known already
    announce = row is None or row.announced_at is None or now - row.announced_at >= repeat
    await _save(session, event_type, subject, False, at, False, now if announce else row.announced_at)
    return announce


async def is_active(session: AsyncSession, event_type: str, subject: str) -> bool:
    row = await _condition(session, event_type, subject)
    return bool(row is not None and row.active)


# ---------------------------------------------------------------- outbox


async def enqueue(
    session: AsyncSession, user_ids: Collection[int], event_type: str, subject: str, message: str, now: datetime
) -> None:
    for user_id in sorted(set(user_ids)):
        await session.execute(
            text(
                "INSERT INTO notification_outbox (user_id, event_type, subject, text, created_at, next_attempt_at) "
                "VALUES (:u, :t, :s, :m, :now, :now)"
            ),
            {"u": user_id, "t": event_type, "s": subject, "m": message, "now": now},
        )


async def due_messages(session: AsyncSession, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    """Messages to send now, locked for this transaction (another sender skips them)."""
    rows = await session.execute(
        text(
            "SELECT o.id, o.user_id, o.text, o.attempts, c.chat_id FROM notification_outbox o "
            "LEFT JOIN telegram_chats c ON c.user_id = o.user_id "
            "WHERE o.sent_at IS NULL AND o.failed_at IS NULL AND o.next_attempt_at <= :now "
            "ORDER BY o.id LIMIT :limit FOR UPDATE OF o SKIP LOCKED"
        ),
        {"now": now, "limit": limit},
    )
    return [dict(r._mapping) for r in rows]


async def mark_sent(session: AsyncSession, message_id: int, now: datetime) -> None:
    await session.execute(
        text("UPDATE notification_outbox SET sent_at = :now, attempts = attempts + 1 WHERE id = :id"),
        {"id": message_id, "now": now},
    )


async def mark_retry(session: AsyncSession, message_id: int, at: datetime, error: str) -> None:
    await session.execute(
        text(
            "UPDATE notification_outbox SET attempts = attempts + 1, next_attempt_at = :at, last_error = :e "
            "WHERE id = :id"
        ),
        {"id": message_id, "at": at, "e": error[:500]},
    )


async def mark_failed(session: AsyncSession, message_id: int, now: datetime, error: str) -> None:
    await session.execute(
        text(
            "UPDATE notification_outbox SET attempts = attempts + 1, failed_at = :now, last_error = :e WHERE id = :id"
        ),
        {"id": message_id, "now": now, "e": error[:500]},
    )


# ---------------------------------------------------------------- cursors


async def cursor(session: AsyncSession, name: str) -> int | None:
    row = (
        await session.execute(text("SELECT position FROM notification_cursors WHERE name = :n"), {"n": name})
    ).first()
    return None if row is None else int(row.position)


async def set_cursor(session: AsyncSession, name: str, position: int) -> None:
    await session.execute(
        text(
            "INSERT INTO notification_cursors (name, position) VALUES (:n, :p) "
            "ON CONFLICT (name) DO UPDATE SET position = :p"
        ),
        {"n": name, "p": position},
    )
