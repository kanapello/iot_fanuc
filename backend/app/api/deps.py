from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import identity
from app.admin import service as admin
from app.core.access import ANALYTICS_VIEW, MACHINE_VIEW, REPAIRS_VIEW, Scope
from app.core.config import Settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request."""
    async with request.app.state.session_factory() as session, session.begin():
        yield session


def get_redis(request: Request) -> Redis | None:
    redis: Redis | None = request.app.state.redis
    return redis


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


Session = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def get_scope(
    session: Session, settings: SettingsDep, authorization: Annotated[str | None, Header()] = None
) -> Scope:
    """The single access check: token -> reachable machines and roles on every node."""
    token = authorization.removeprefix("Bearer ").strip() if authorization else None
    scope = await identity.load_scope(session, settings, token)
    if scope is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return scope


CurrentScope = Annotated[Scope, Depends(get_scope)]

NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "not found")
FORBIDDEN = HTTPException(status.HTTP_403_FORBIDDEN, "not allowed")


def require(scope: Scope, permission: str) -> None:
    """The caller must hold the permission somewhere, otherwise the whole section is closed to them."""
    if not scope.has(permission):
        raise FORBIDDEN


def require_machine(scope: Scope, machine_id: int, permission: str = MACHINE_VIEW) -> None:
    """A machine outside the scope does not exist for the caller (404); inside it the action may be forbidden (403)."""
    if not scope.can_machine(MACHINE_VIEW, machine_id):
        raise NOT_FOUND
    if not scope.can_machine(permission, machine_id):
        raise FORBIDDEN


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


From = Annotated[datetime, Query(alias="from")]
To = Annotated[datetime | None, Query(alias="to")]


def time_range(start: datetime, end: datetime | None) -> tuple[datetime, datetime, datetime]:
    """(from, to, now) of a period in UTC; ``to`` defaults to now."""
    now = datetime.now(UTC)
    start, end = as_utc(start), as_utc(end) if end else now
    if end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' must be after 'from'")
    return start, end, now


class VoidBody(BaseModel):
    """A record entered by mistake is voided with a reason instead of being deleted."""

    reason: str = Field(min_length=3, max_length=500)


def machines_with(permission: str) -> Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]:
    """Dependency: machines the caller may reach with ``permission``, narrowed by query parameters.

    Filters can only narrow the scope: naming someone else's machine, site or
    area simply selects nothing.
    """

    async def select(
        scope: CurrentScope,
        session: Session,
        machine_id: Annotated[list[int] | None, Query()] = None,
        site_id: Annotated[int | None, Query()] = None,
        area_id: Annotated[int | None, Query()] = None,
    ) -> list[dict[str, Any]]:
        require(scope, permission)
        machines = await admin.list_machines(session, scope.machine_ids(permission))
        if machine_id:
            machines = [m for m in machines if m["id"] in machine_id]
        if site_id is not None:
            machines = [m for m in machines if m["site_id"] == site_id]
        if area_id is not None:
            machines = [m for m in machines if m["area_id"] == area_id]
        return machines

    return select


ViewMachines = Annotated[list[dict[str, Any]], Depends(machines_with(MACHINE_VIEW))]
AnalyticsMachines = Annotated[list[dict[str, Any]], Depends(machines_with(ANALYTICS_VIEW))]
RepairsMachines = Annotated[list[dict[str, Any]], Depends(machines_with(REPAIRS_VIEW))]
