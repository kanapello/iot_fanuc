"""Profile of the signed-in user: the Telegram chat for notifications and the event types (ADR 0011).

Everything here concerns the caller's own account only; kiosks and TV dashboards have no profile.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import FORBIDDEN, CurrentScope, Session, SettingsDep
from app.core.access import Scope
from app.notify import store
from app.notify.events import eligible_types

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


def _user_id(scope: Scope) -> int:
    if scope.kind != "user" or scope.user_id is None:
        raise FORBIDDEN
    return scope.user_id


def _types(scope: Scope) -> list[str]:
    return eligible_types({g.role for g in scope.grants})


@router.get("/telegram")
async def telegram_status(scope: CurrentScope, session: Session, settings: SettingsDep) -> dict[str, Any]:
    chat = await store.chat_of(session, _user_id(scope))
    return {
        "name": scope.name,
        "configured": bool(settings.telegram_bot_token and settings.telegram_bot_username),
        "bot": settings.telegram_bot_username,
        "linked": chat is not None,
        "username": chat["username"] if chat else None,
        "linked_at": chat["linked_at"] if chat else None,
    }


@router.post("/telegram/link")
async def telegram_link(scope: CurrentScope, session: Session, settings: SettingsDep) -> dict[str, Any]:
    """A one-time link t.me/<bot>?start=<code>, valid for 15 minutes: opening it in Telegram links the chat."""
    user_id = _user_id(scope)
    if not (settings.telegram_bot_token and settings.telegram_bot_username):
        raise HTTPException(status.HTTP_409_CONFLICT, "the telegram bot is not configured")
    code, expires = await store.new_link_code(session, user_id, datetime.now(UTC))
    return {"url": f"https://t.me/{settings.telegram_bot_username}?start={code}", "expires_at": expires}


@router.delete("/telegram")
async def telegram_unlink(scope: CurrentScope, session: Session) -> dict[str, Any]:
    await store.unlink(session, _user_id(scope))
    return {"name": scope.name, "linked": False}


@router.get("/notifications")
async def notification_types(scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    """Event types of the caller's roles and whether each is on (all are on by default)."""
    muted = await store.muted_types(session, _user_id(scope))
    return [{"type": event_type, "enabled": event_type not in muted} for event_type in _types(scope)]


class NotificationsIn(BaseModel):
    enabled: dict[str, bool]


@router.put("/notifications")
async def set_notification_types(body: NotificationsIn, scope: CurrentScope, session: Session) -> list[dict[str, Any]]:
    user_id = _user_id(scope)
    unknown = set(body.enabled) - set(_types(scope))
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"not event types of your roles: {sorted(unknown)}")
    await store.set_muted(session, user_id, list(body.enabled), [t for t, on in body.enabled.items() if not on])
    return await notification_types(scope, session)
