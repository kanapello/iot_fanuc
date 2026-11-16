"""Skeleton auth: one administrator (JWT) and one shared device token for kiosks and TV dashboards."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings

ALGORITHM = "HS256"


@dataclass(frozen=True, slots=True)
class Principal:
    name: str
    role: str  # "admin" | "device"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def login(settings: Settings, username: str, password: str) -> str | None:
    ok = hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )
    if not ok:
        return None
    expires = datetime.now(UTC) + timedelta(hours=settings.token_ttl_hours)
    return jwt.encode({"sub": username, "role": "admin", "exp": expires}, settings.secret_key, algorithm=ALGORITHM)


def authenticate(settings: Settings, token: str | None) -> Principal | None:
    if not token:
        return None
    if hmac.compare_digest(token, settings.device_token):
        return Principal("device", "device")
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    return Principal(str(claims.get("sub", "")), str(claims.get("role", "")))
