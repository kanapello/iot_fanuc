from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
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
TIMESCALE_IMAGE = "timescale/timescaledb:2.32.0-pg16"

DEFAULT_RULES = {
    "offline_timeout_s": 30,
    "idle_to_stop_s": 120,
    "min_interval_s": 5,
    "run_modes": ["MEM", "RMT", "DNC"],
    "setup_modes": ["EDIT", "MDI", "JOG", "HND"],
}


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
        data = {"version": "0.1.0", "uptime_s": 12.5, "buffer_size": 0, "epoch": "e1", "machines": {"m1": True}}
    body: dict[str, Any] = {
        "schema": f"{kind}.v1",
        "msg_id": str(uuid.uuid4()),
        "collector_id": "c1",
        "machine_id": "m1",
        "ts": "2026-11-16T19:01:02.345Z",
        "seq": 1,
        "data": data,
    }
    if kind == "heartbeat":
        del body["machine_id"]
    body.update(overrides)
    return body


def topic_for(body: dict[str, Any], org: str = "torg", site: str = "t1") -> str:
    kind = body["schema"].split(".")[0]
    if kind == "heartbeat":
        return f"mm/v1/{org}/{site}/collector/{body['collector_id']}/heartbeat"
    return f"mm/v1/{org}/{site}/{body['machine_id']}/{kind}"


def encode(body: dict[str, Any], org: str = "torg", site: str = "t1") -> tuple[str, bytes]:
    return topic_for(body, org, site), json.dumps(body).encode()


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
    """An empty installation: the demo seed of the migrations is removed."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE organizations, users, telemetry, machine_events, ingest_rejects, state_intervals, "
                "part_events, timeline_dirty, oee_dirty, oee_hourly, oee_shift RESTART IDENTITY CASCADE"
            )
        )
    return create_session_factory(engine)


@pytest.fixture
def service(session_factory: async_sessionmaker[AsyncSession], validator: MessageValidator) -> IngestService:
    return IngestService(session_factory, validator)


@dataclass
class Seed:
    machine: dict[str, Any]
    org_id: int
    site_id: int
    area_id: int
    group_id: int  # downtime reason group "planned"
    planned_reason_id: int
    unplanned_reason_id: int

    @property
    def machine_id(self) -> int:
        return int(self.machine["id"])


async def seed_reference(
    factory: async_sessionmaker[AsyncSession],
    org: str = "torg",
    site: str = "t1",
    machine: str = "m1",
    collector: str = "c1",
    norm_s: float = 45.0,
    timezone: str = "UTC",
) -> Seed:
    """An organization with one site (round-the-clock shift), one area, one collector and one machine."""
    from app.admin import service as admin

    async with factory() as session, session.begin():

        async def one(sql: str, **params: Any) -> int:
            return int((await session.execute(text(sql), params)).scalar_one())

        org_id = await one(
            "INSERT INTO organizations (code, name) VALUES (:c, :c) ON CONFLICT (code) DO UPDATE SET name = :c "
            "RETURNING id",
            c=org,
        )
        site_id = await one(
            "INSERT INTO sites (org_id, code, name, timezone) VALUES (:o, :c, :n, :tz) RETURNING id",
            o=org_id, c=site, n=f"{org}/{site}", tz=timezone,
        )  # fmt: skip
        area_id = await one(
            "INSERT INTO areas (site_id, name) VALUES (:s, :n) RETURNING id", s=site_id, n=f"{site} area"
        )
        await session.execute(
            text("INSERT INTO collectors (id, site_id, name) VALUES (:id, :s, :id)"), {"id": collector, "s": site_id}
        )
        machine_id = await one(
            "INSERT INTO machines (site_id, area_id, code, name, collector_id) VALUES (:s, :a, :c, :n, :col) "
            "RETURNING id",
            s=site_id, a=area_id, c=machine, n=f"{org} {machine}", col=collector,
        )  # fmt: skip
        if await one("SELECT count(*) FROM shift_templates WHERE org_id = :o", o=org_id) == 0:
            await session.execute(
                text(
                    "INSERT INTO shift_templates (org_id, name, start_time, end_time) "
                    "VALUES (:o, 'Round the clock', '00:00', '00:00')"
                ),
                {"o": org_id},
            )
            await session.execute(
                text("INSERT INTO programs (org_id, program, ideal_cycle_time_s, valid_from) "
                     "VALUES (:o, 'O1', :norm, '2000-01-01T00:00:00Z')"),
                {"o": org_id, "norm": norm_s},
            )  # fmt: skip
            await session.execute(
                text("INSERT INTO state_rules (org_id, name, rules) VALUES (:o, 'Default', CAST(:r AS jsonb))"),
                {"o": org_id, "r": json.dumps(DEFAULT_RULES)},
            )
            group = await one(
                "INSERT INTO downtime_reasons (org_id, code, name, category) VALUES (:o, 'PLAN', :n, 'planned') "
                "RETURNING id",
                o=org_id, n=f"{org} planned",
            )  # fmt: skip
            await session.execute(
                text(
                    "INSERT INTO downtime_reasons (org_id, parent_id, code, name, category) VALUES "
                    "(:o, :g, 'CHANGEOVER', :n1, 'planned'), (:o, :g, 'NO_MATERIAL', :n2, 'unplanned')"
                ),
                {"o": org_id, "g": group, "n1": f"{org} changeover", "n2": f"{org} no material"},
            )
            await session.execute(
                text("INSERT INTO scrap_reasons (org_id, code, name) VALUES (:o, 'SIZE', :n)"),
                {"o": org_id, "n": f"{org} size"},
            )
        group_id = await one("SELECT id FROM downtime_reasons WHERE org_id = :o AND code = 'PLAN'", o=org_id)
        planned = await one("SELECT id FROM downtime_reasons WHERE org_id = :o AND code = 'CHANGEOVER'", o=org_id)
        unplanned = await one("SELECT id FROM downtime_reasons WHERE org_id = :o AND code = 'NO_MATERIAL'", o=org_id)
    async with factory() as session:
        (row,) = await admin.list_machines(session, [machine_id])
    return Seed(row, org_id, site_id, area_id, group_id, planned, unplanned)


async def put_samples(
    factory: async_sessionmaker[AsyncSession],
    machine_id: int,
    samples: list[dict[str, Any]],
    received_at: Any = None,
    collector: str = "c1",
) -> None:
    """Insert raw telemetry directly and leave the mark ingest would leave."""
    from app.timeline.service import mark_dirty

    rows = [
        {
            "msg_id": uuid.uuid4(),
            "machine_id": machine_id,
            "collector_id": collector,
            "seq": i + 1,
            "connected": True,
            "mode": "MEM",
            "run": "STOP",
            "alarm": False,
            "emergency": False,
            "program": "O1",
            "part_count": None,
            "received_at": received_at,
            **sample,
        }
        for i, sample in enumerate(samples)
    ]
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO telemetry (msg_id, ts, machine_id, collector_id, seq, connected, mode, run, alarm, "
                "emergency, program, part_count, received_at) VALUES (:msg_id, :ts, :machine_id, :collector_id, :seq, "
                ":connected, :mode, :run, :alarm, :emergency, :program, :part_count, COALESCE(:received_at, now()))"
            ),
            rows,
        )
        await mark_dirty(session, {machine_id: min(r["ts"] for r in rows)})
