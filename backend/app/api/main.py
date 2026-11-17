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

from app.admin import identity
from app.admin import service as admin
from app.api.routes import router
from app.core.config import get_settings
from app.core.db import create_engine, create_session_factory
from app.core.logging import setup_logging
from app.worker.live import live_channel, read_live

log = logging.getLogger("app.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = create_engine(settings.database_url)
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    async with app.state.session_factory() as session, session.begin():
        await identity.bootstrap(session, settings)
    yield
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(title="Machine Monitor API", version="0.3.0", lifespan=lifespan)
app.include_router(router)


@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket, token: str | None = None) -> None:
    """Live state of the machines in the caller's scope: a snapshot, then every change."""
    async with websocket.app.state.session_factory() as session:
        scope = await identity.load_scope(session, websocket.app.state.settings, token)
        if scope is None:
            # accept first: a close code is visible to the browser only on an open socket
            await websocket.accept()
            await websocket.close(code=4401)
            return
        allowed = scope.machine_ids()
        machines = await admin.list_machines(session, allowed)
    await websocket.accept()
    redis: Redis = websocket.app.state.redis
    live = await read_live(redis, [m["id"] for m in machines])
    await websocket.send_text(json.dumps({"type": "snapshot", "machines": list(live.values())}, ensure_ascii=False))
    # one channel per site; within a site the scope may still cover only one area or machine
    channels = sorted({live_channel(m["org_code"], m["site_code"]) for m in machines})

    async def forward() -> None:
        if not channels:
            return
        async with redis.pubsub() as pubsub:
            await pubsub.subscribe(*channels)
            async for message in pubsub.listen():
                if message["type"] == "message" and json.loads(message["data"])["machine_id"] in allowed:
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
