"""Passwords, session tokens (JWT) and device tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings

ALGORITHM = "HS256"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=len(expected), **_SCRYPT)
    return hmac.compare_digest(digest, expected)


def new_device_token() -> str:
    return "dev_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Device tokens are long random strings, so a plain SHA-256 is enough; only the hash is stored."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_session_token(settings: Settings, user_id: int) -> str:
    expires = datetime.now(UTC) + timedelta(hours=settings.token_ttl_hours)
    return jwt.encode({"sub": str(user_id), "exp": expires}, settings.secret_key, algorithm=ALGORITHM)


def read_session_token(settings: Settings, token: str) -> int | None:
    """User id from a valid session token."""
    try:
        claims = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
