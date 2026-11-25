"""The minimum the API needs on the internet (ADR 0010): security headers, the client address,
and a limit on failed logins kept in Redis."""

from __future__ import annotations

import hashlib
import logging

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("app.api")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class SecurityHeadersMiddleware:
    """Adds the security headers to every HTTP response of the API, errors included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def client_ip(request: Request) -> str:
    """Address of the browser. The API is reachable only through the reverse proxy in front of it
    (Caddy in production, nginx of the web container otherwise); the proxy overwrites X-Real-IP
    with the address it was connected from, so a client cannot choose it."""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _key(ip: str, username: str) -> str:
    login = hashlib.sha256(username.encode()).hexdigest()[:32]  # fixed length, no logins in Redis
    return f"mm:login-failures:{ip}:{login}"


class LoginGuard:
    """Failed logins per (address, login) in a fixed window. Without Redis the limit is off (logged):
    a cache outage must not lock everyone out."""

    def __init__(self, redis: Redis | None, max_failures: int, window_s: int) -> None:
        self._redis = redis
        self._max = max_failures
        self._window = window_s

    async def retry_after(self, ip: str, username: str) -> int | None:
        """Seconds until the next attempt is allowed, or None if it is allowed now."""
        if self._redis is None:
            return None
        key = _key(ip, username)
        try:
            failures = await self._redis.get(key)
            if failures is None or int(failures) < self._max:
                return None
            return max(int(await self._redis.ttl(key)), 1)
        except RedisError:
            log.warning("login limit unavailable: Redis error", exc_info=True)
            return None

    async def failed(self, ip: str, username: str) -> None:
        if self._redis is None:
            return
        key = _key(ip, username)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, self._window, nx=True)  # the window starts with the first failure
                await pipe.execute()
        except RedisError:
            log.warning("login limit unavailable: Redis error", exc_info=True)

    async def succeeded(self, ip: str, username: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(_key(ip, username))
        except RedisError:
            log.warning("login limit unavailable: Redis error", exc_info=True)
