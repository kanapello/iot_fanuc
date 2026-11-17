from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from app.core.db import create_engine, create_session_factory
from app.ingest.contract import MessageValidator
from app.ingest.service import IngestService

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = BACKEND_DIR.parent / "shared" / "schemas"
TIMESCALE_IMAGE = "timescale/timescaledb:latest-pg16"


def make_message(kind: str = "telemetry", **overrides: Any) -> dict[str, Any]:
    """A valid v1 message; ``overrides`` replace top-level fields."""
    data: dict[str, Any]
    if kind == "telemetry":
        data = {
            "connected": True,
            "mode": "MEM",
            "run": "START",
            "motion": True,
            "emergency": False,
            "alarm": False,
            "program": "O1234",
            "program_name": "O1234",
            "feed_actual": 1250.0,
            "spindle_speed": 8000,
            "spindle_load": 37.5,
            "feed_override": 100,
            "spindle_override": 100,
            "part_count": 15873,
        }
    elif kind == "event":
        data = {"type": "alarm_raised", "code": "SV0401", "message": "V-READY OFF", "category": "servo"}
    else:
        data = {"version": "0.1.0", "uptime_s": 12.5, "buffer_size": 0, "machines": {"m07": True}}
    body: dict[str, Any] = {
        "schema": f"{kind}.v1",
        "msg_id": str(uuid.uuid4()),
        "collector_id": "edge-shop1",
        "machine_id": "m07",
        "ts": "2026-11-16T19:01:02.345Z",
        "seq": 1,
        "data": data,
    }
    if kind == "heartbeat":
        del body["machine_id"]
    body.update(overrides)
    return body


def topic_for(body: dict[str, Any], site: str = "plant1") -> str:
    kind = body["schema"].split(".")[0]
    if kind == "heartbeat":
        return f"mm/v1/{site}/collector/{body['collector_id']}/heartbeat"
    return f"mm/v1/{site}/{body['machine_id']}/{kind}"


def encode(body: dict[str, Any], site: str = "plant1") -> tuple[str, bytes]:
    return topic_for(body, site), json.dumps(body).encode()


@pytest.fixture(scope="session")
def validator() -> MessageValidator:
    return MessageValidator(SCHEMAS_DIR)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Real PostgreSQL + TimescaleDB with all Alembic migrations applied."""
    with PostgresContainer(TIMESCALE_IMAGE, driver="asyncpg") as container:
        url: str = container.get_connection_url()
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        yield url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE telemetry, machine_events, ingest_rejects, machines, collectors, areas, sites CASCADE")
        )
    return create_session_factory(engine)


@pytest.fixture
def service(session_factory: async_sessionmaker[AsyncSession], validator: MessageValidator) -> IngestService:
    return IngestService(session_factory, validator)
