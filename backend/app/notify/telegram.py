"""A thin client of the Telegram Bot API: sendMessage and getUpdates (long polling, no public address)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

# httpx logs every request URL at INFO, and the URL of the Bot API contains the token
logging.getLogger("httpx").setLevel(logging.WARNING)

LONG_POLL_S = 50


class TelegramError(Exception):
    """A call failed: ``status`` 0 for a network error; ``retry_after`` when Telegram asks to wait (429)."""

    def __init__(self, status: int, description: str, retry_after: int | None = None) -> None:
        super().__init__(f"{status}: {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


class TelegramApi:
    def __init__(self, token: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/bot{token}/", transport=transport, timeout=LONG_POLL_S + 15
        )

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***")

    async def call(self, method: str, **params: Any) -> Any:
        try:
            response = await self._client.post(method, json=params)
        except httpx.HTTPError as exc:
            raise TelegramError(0, self._scrub(f"{type(exc).__name__}: {exc}")) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            description = str(body.get("description") or response.reason_phrase or "error")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            raise TelegramError(response.status_code, self._scrub(description), retry_after)
        return body.get("result")

    async def send_message(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)

    async def get_updates(self, offset: int, timeout_s: int = LONG_POLL_S) -> list[dict[str, Any]]:
        result = await self.call("getUpdates", offset=offset, timeout=timeout_s, allowed_updates=["message"])
        return list(result or [])

    async def aclose(self) -> None:
        await self._client.aclose()
