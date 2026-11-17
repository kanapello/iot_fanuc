from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import service as admin
from app.core.config import Settings
from app.core.security import Principal, authenticate


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request."""
    async with request.app.state.session_factory() as session, session.begin():
        yield session


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_principal(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    principal = authenticate(settings, token)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return principal


def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator role required")
    return principal


Session = Annotated[AsyncSession, Depends(get_session)]
User = Annotated[Principal, Depends(get_principal)]
Admin = Annotated[Principal, Depends(require_admin)]


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def select_machines(
    session: Session,
    machine_id: Annotated[list[str] | None, Query()] = None,
    area_id: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    """Machines chosen by ?machine_id=...&machine_id=... or ?area_id=...; all active by default."""
    machines = await admin.list_machines(session)
    if machine_id:
        machines = [m for m in machines if m["id"] in machine_id]
    if area_id is not None:
        machines = [m for m in machines if m["area_id"] == area_id]
    return machines


Machines = Annotated[list[dict[str, Any]], Depends(select_machines)]
