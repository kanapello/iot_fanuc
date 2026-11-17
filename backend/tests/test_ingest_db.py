"""Ingest against a real PostgreSQL + TimescaleDB (testcontainers)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest.service import IngestService
from tests.conftest import encode, make_message

Factory = async_sessionmaker[AsyncSession]


async def scalar(factory: Factory, sql: str) -> Any:
    async with factory() as session:
        return (await session.execute(text(sql))).scalar_one()


async def test_tables_are_hypertables_with_policies(session_factory: Factory) -> None:
    async with session_factory() as session:
        hypertables = (
            await session.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables ORDER BY 1"))
        ).scalars()
        assert list(hypertables) == ["machine_events", "telemetry"]
        jobs = (
            await session.execute(
                text(
                    "SELECT hypertable_name || ':' || proc_name FROM timescaledb_information.jobs "
                    "WHERE hypertable_name IS NOT NULL ORDER BY 1"
                )
            )
        ).scalars()
        assert list(jobs) == [
            "machine_events:policy_retention",
            "telemetry:policy_compression",
            "telemetry:policy_retention",
        ]


async def test_telemetry_is_stored_with_all_signals(service: IngestService, session_factory: Factory) -> None:
    body = make_message("telemetry", seq=184223)
    result = await service.handle_batch([encode(body)])
    assert (result.telemetry_inserted, result.duplicates, result.rejected) == (1, 0, 0)

    async with session_factory() as session:
        row = (await session.execute(text("SELECT * FROM telemetry"))).mappings().one()
    assert str(row["msg_id"]) == body["msg_id"]
    assert row["ts"] == datetime(2026, 11, 16, 19, 1, 2, 345000, tzinfo=UTC)
    assert (row["machine_id"], row["collector_id"], row["seq"]) == ("m07", "edge-shop1", 184223)
    assert (row["mode"], row["run"], row["motion"], row["alarm"]) == ("MEM", "START", True, False)
    assert (row["program"], row["part_count"], row["spindle_load"]) == ("O1234", 15873, 37.5)
    assert row["received_at"] is not None


async def test_redelivery_changes_nothing(service: IngestService, session_factory: Factory) -> None:
    first, second = make_message("telemetry", seq=1), make_message("event", seq=2)
    batch = [encode(first), encode(second)]
    await service.handle_batch(batch)
    received_at = await scalar(session_factory, "SELECT received_at FROM telemetry")

    # same batch again, and the same message twice inside one batch
    again = await service.handle_batch([*batch, encode(first)])
    assert (again.telemetry_inserted, again.events_inserted, again.duplicates) == (0, 0, 3)
    assert await scalar(session_factory, "SELECT count(*) FROM telemetry") == 1
    assert await scalar(session_factory, "SELECT count(*) FROM machine_events") == 1
    assert await scalar(session_factory, "SELECT received_at FROM telemetry") == received_at


async def test_duplicate_inside_first_batch(service: IngestService, session_factory: Factory) -> None:
    message = encode(make_message("telemetry"))
    result = await service.handle_batch([message, message])
    assert (result.telemetry_inserted, result.duplicates) == (1, 1)
    assert await scalar(session_factory, "SELECT count(*) FROM telemetry") == 1


async def test_invalid_messages_go_to_rejects(service: IngestService, session_factory: Factory) -> None:
    good = encode(make_message("telemetry"))
    bad = ("mm/v1/plant1/m07/telemetry", json.dumps(make_message("telemetry", seq="x")).encode())
    result = await service.handle_batch([good, bad, ("mm/v1/plant1/m07/telemetry", b"garbage")])
    assert (result.telemetry_inserted, result.rejected) == (1, 2)
    async with session_factory() as session:
        reasons = (await session.execute(text("SELECT reason FROM ingest_rejects ORDER BY id"))).scalars().all()
    assert "schema violation at seq" in reasons[0]
    assert reasons[1] == "payload is not valid JSON"


async def test_event_payload_is_kept(service: IngestService, session_factory: Factory) -> None:
    await service.handle_batch([encode(make_message("event"))])
    async with session_factory() as session:
        row = (await session.execute(text("SELECT type, code, message, payload FROM machine_events"))).one()
    assert (row.type, row.code, row.message) == ("alarm_raised", "SV0401", "V-READY OFF")
    assert row.payload["category"] == "servo"


async def test_machines_are_auto_registered_once(service: IngestService, session_factory: Factory) -> None:
    await service.handle_batch([encode(make_message("telemetry", machine_id=m)) for m in ("m07", "m08", "m07")])
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE machines SET name = 'Фрезерный 07' WHERE id = 'm07'"))

    # a fresh service (ingest restart) must not overwrite what the administrator edited
    restarted = IngestService(session_factory, service._validator)
    await restarted.handle_batch([encode(make_message("telemetry", machine_id="m07"))])
    async with session_factory() as session:
        machines = (
            await session.execute(text("SELECT id, name, site_id, collector_id FROM machines ORDER BY id"))
        ).all()
    assert [tuple(m) for m in machines] == [
        ("m07", "Фрезерный 07", "plant1", "edge-shop1"),
        ("m08", "m08", "plant1", "edge-shop1"),
    ]
    assert await scalar(session_factory, "SELECT count(*) FROM sites") == 1


async def test_heartbeat_updates_collector(service: IngestService, session_factory: Factory) -> None:
    newer = make_message("heartbeat", ts="2026-11-16T19:05:00.000Z")
    older = make_message("heartbeat", ts="2026-11-16T19:00:00.000Z", data={"version": "0.0.9", "uptime_s": 1})
    result = await service.handle_batch([encode(newer), encode(older)])
    assert result.heartbeats == 2
    await service.handle_batch([encode(older)])  # late redelivery must not move the clock back
    async with session_factory() as session:
        row = (await session.execute(text("SELECT version, last_heartbeat FROM collectors"))).one()
    assert row.version == "0.1.0"
    assert row.last_heartbeat == datetime(2026, 11, 16, 19, 5, tzinfo=UTC)
