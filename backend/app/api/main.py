"""FastAPI application: REST + WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.admin import service as admin
from app.api.routes import router
from app.core.config import get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import setup_logging
from app.core.security import authenticate
from app.worker.live import read_live

log = logging.getLogger("app.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    yield
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(title="Machine Monitor API", version="0.2.0", lifespan=lifespan)
app.include_router(router)


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket, token: str | None = None, site: str | None = None) -> None:
    """Live machine state: a snapshot first, then every change published by the worker."""
    if authenticate(websocket.app.state.settings, token) is None:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    redis: Redis = websocket.app.state.redis
    async with websocket.app.state.session_factory() as session:
        machines = [m for m in await admin.list_machines(session) if site in (None, m["site_id"])]
    live = await read_live(redis, [m["id"] for m in machines])
    await websocket.send_text(json.dumps({"type": "snapshot", "machines": list(live.values())}, ensure_ascii=False))

    async def forward() -> None:
        async with redis.pubsub() as pubsub:
            await pubsub.psubscribe(f"live.{site or '*'}")
            async for message in pubsub.listen():
                if message["type"] == "pmessage":
                    await websocket.send_text('{"type":"update","machine":' + message["data"] + "}")

    task = asyncio.create_task(forward())
    try:
        while True:
            await websocket.receive_text()  # clients send nothing; this detects disconnects
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
