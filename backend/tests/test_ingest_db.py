"""Ingest against a real PostgreSQL + TimescaleDB (testcontainers)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingest.contract import MessageValidator
from app.ingest.service import IngestService
from tests.conftest import encode, make_message, seed_reference

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
    seed = await seed_reference(session_factory)
    body = make_message("telemetry", seq=184223)
    result = await service.handle_batch([encode(body)])
    assert (result.telemetry_inserted, result.duplicates, result.rejected) == (1, 0, 0)

    async with session_factory() as session:
        row = (await session.execute(text("SELECT * FROM telemetry"))).mappings().one()
    assert str(row["msg_id"]) == body["msg_id"]
    assert row["ts"] == datetime(2026, 11, 16, 19, 1, 2, 345000, tzinfo=UTC)
    assert (row["machine_id"], row["collector_id"], row["seq"]) == (seed.machine_id, "c1", 184223)
    assert (row["mode"], row["run"], row["motion"], row["alarm"]) == ("MEM", "START", True, False)
    assert (row["program"], row["part_count"], row["spindle_load"]) == ("O1234", 15873, 37.5)
    assert row["received_at"] is not None
    assert await scalar(session_factory, "SELECT count(*) FROM timeline_dirty") == 1


async def test_redelivery_changes_nothing(service: IngestService, session_factory: Factory) -> None:
    await seed_reference(session_factory)
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


async def test_invalid_messages_go_to_rejects(service: IngestService, session_factory: Factory) -> None:
    await seed_reference(session_factory)
    good = encode(make_message("telemetry"))
    bad = ("mm/v1/torg/t1/m1/telemetry", json.dumps(make_message("telemetry", seq="x")).encode())
    result = await service.handle_batch([good, bad, ("mm/v1/torg/t1/m1/telemetry", b"garbage")])
    assert (result.telemetry_inserted, result.rejected) == (1, 2)
    async with session_factory() as session:
        reasons = (await session.execute(text("SELECT reason FROM ingest_rejects ORDER BY id"))).scalars().all()
    assert "schema violation at seq" in reasons[0]
    assert reasons[1] == "payload is not valid JSON"


async def test_event_payload_is_kept(service: IngestService, session_factory: Factory) -> None:
    await seed_reference(session_factory)
    await service.handle_batch([encode(make_message("event"))])
    async with session_factory() as session:
        row = (await session.execute(text("SELECT type, code, message, payload FROM machine_events"))).one()
    assert (row.type, row.code, row.message) == ("alarm_raised", "SV0401", "V-READY OFF")
    assert row.payload["category"] == "servo"


async def test_machines_are_registered_only_inside_the_collectors_site(
    service: IngestService, session_factory: Factory
) -> None:
    seed = await seed_reference(session_factory)
    other = await seed_reference(session_factory, org="other", site="t1", machine="m1", collector="c2")
    await service.handle_batch([encode(make_message("telemetry", machine_id=m)) for m in ("m1", "m9", "m9")])
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE machines SET name = 'Фрезерный 9' WHERE code = 'm9'"))

    # a fresh service (ingest restart) must not overwrite what the administrator edited
    restarted = IngestService(session_factory, service._validator)
    await restarted.handle_batch([encode(make_message("telemetry", machine_id="m9"))])
    async with session_factory() as session:
        machines = (
            await session.execute(text("SELECT site_id, code, name, collector_id FROM machines ORDER BY id"))
        ).all()
    assert [tuple(m) for m in machines] == [
        (seed.site_id, "m1", "torg m1", "c1"),
        (other.site_id, "m1", "other m1", "c2"),  # the same code in another organization is another machine
        (seed.site_id, "m9", "Фрезерный 9", "c1"),
    ]
    assert await scalar(session_factory, "SELECT count(DISTINCT machine_id) FROM telemetry") == 2


async def test_collector_cannot_write_into_another_site(service: IngestService, session_factory: Factory) -> None:
    """A collector is accepted only on topics of its own organization and site."""
    mine = await seed_reference(session_factory)
    other = await seed_reference(session_factory, org="other", site="t9", machine="m1", collector="c2")

    foreign_site = encode(make_message("telemetry"), org="other", site="t9")  # c1 into the site of c2
    foreign_org = encode(make_message("telemetry"), org="other", site="t1")
    unknown = encode(make_message("telemetry", collector_id="ghost"))
    foreign_heartbeat = encode(make_message("heartbeat"), org="other", site="t9")
    result = await service.handle_batch([foreign_site, foreign_org, unknown, foreign_heartbeat])
    assert (result.telemetry_inserted, result.heartbeats, result.rejected) == (0, 0, 4)
    assert await scalar(session_factory, "SELECT count(*) FROM telemetry") == 0
    assert await scalar(session_factory, "SELECT count(*) FROM machines") == 2  # nothing was auto-registered
    assert await scalar(session_factory, "SELECT last_heartbeat FROM collectors WHERE id = 'c2'") is None
    async with session_factory() as session:
        reasons = (await session.execute(text("SELECT reason FROM ingest_rejects ORDER BY id"))).scalars().all()
    assert reasons == [
        "collector does not belong to the organization/site of the topic",
        "collector does not belong to the organization/site of the topic",
        "unknown collector",
        "collector does not belong to the organization/site of the topic",
    ]

    # each collector on its own topic is fine, and lands on its own machine
    ok = await service.handle_batch(
        [
            encode(make_message("telemetry")),
            encode(make_message("telemetry", collector_id="c2"), org="other", site="t9"),
        ]
    )
    assert ok.telemetry_inserted == 2
    async with session_factory() as session:
        stored = (await session.execute(text("SELECT collector_id, machine_id FROM telemetry ORDER BY 1"))).all()
    assert [tuple(r) for r in stored] == [("c1", mine.machine_id), ("c2", other.machine_id)]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_moved_or_deleted_collector_stops_writing_into_its_old_place(
    session_factory: Factory, validator: MessageValidator
) -> None:
    """Collectors and machines are reloaded every 30 s: the cache does not keep old places alive."""
    t1 = await seed_reference(session_factory, site="t1", machine="m1", collector="c1")
    t2 = await seed_reference(session_factory, site="t2", machine="m2", collector="c2")
    clock = Clock()
    service = IngestService(session_factory, validator, refresh_s=30, clock=clock)
    seq = iter(range(1, 1000))

    def from_c1(site: str) -> tuple[str, bytes]:
        return encode(make_message("telemetry", seq=next(seq)), site=site)

    def from_c2(machine: str = "m2") -> tuple[str, bytes]:
        return encode(make_message("telemetry", collector_id="c2", machine_id=machine, seq=next(seq)), site="t2")

    async def rows_of(machine_id: int) -> int:
        return int(await scalar(session_factory, f"SELECT count(*) FROM telemetry WHERE machine_id = {machine_id}"))

    assert (await service.handle_batch([from_c1("t1"), from_c2()])).telemetry_inserted == 2

    # the administrator moves c1 to site t2
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE collectors SET site_id = :s WHERE id = 'c1'"), {"s": t2.site_id})
    clock.now += 10  # within the refresh period the cached place is still used
    assert (await service.handle_batch([from_c1("t1")])).telemetry_inserted == 1
    clock.now += 21  # 31 s since the directories were loaded
    moved = await service.handle_batch([from_c1("t1"), from_c1("t2")])
    assert (moved.telemetry_inserted, moved.rejected) == (1, 1)
    assert await rows_of(t1.machine_id) == 2  # nothing new on the old site after the refresh
    new_m1 = await scalar(session_factory, f"SELECT id FROM machines WHERE site_id = {t2.site_id} AND code = 'm1'")
    assert await rows_of(new_m1) == 1  # c1 now writes to machine m1 of site t2

    # the administrator gives m2 to c1 and deletes c2
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE machines SET collector_id = 'c1' WHERE id = :m"), {"m": t2.machine_id})
        await session.execute(text("DELETE FROM collectors WHERE id = 'c2'"))
    clock.now += 31
    gone = await service.handle_batch([from_c2(), from_c2("m7")])  # m7 would be a new machine of c2
    assert (gone.telemetry_inserted, gone.rejected) == (0, 2)
    assert await rows_of(t2.machine_id) == 1
    assert await scalar(session_factory, "SELECT count(*) FROM machines WHERE code = 'm7'") == 0
    async with session_factory() as session:
        reasons = (await session.execute(text("SELECT reason FROM ingest_rejects ORDER BY id"))).scalars().all()
    assert reasons == [
        "collector does not belong to the organization/site of the topic",
        "unknown collector",
        "unknown collector",
    ]


async def test_heartbeat_and_lost_buffer(
    service: IngestService, session_factory: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    await seed_reference(session_factory)

    def heartbeat(ts: str, epoch: str, version: str = "0.1.0") -> tuple[str, bytes]:
        data = {"version": version, "uptime_s": 1, "epoch": epoch}
        return encode(make_message("heartbeat", ts=ts, data=data))

    async def collector() -> Any:
        async with session_factory() as session:
            return (
                await session.execute(text("SELECT version, last_heartbeat, epoch, buffer_lost_at FROM collectors"))
            ).one()

    with caplog.at_level(logging.WARNING, logger="app.ingest.service"):
        newer, older = heartbeat("2026-11-16T19:05:00.000Z", "e1"), heartbeat("2026-11-16T19:00:00.000Z", "e1", "0.0.9")
        assert (await service.handle_batch([newer, older])).heartbeats == 2
        await service.handle_batch([older])  # late redelivery must not move the clock back
        row = await collector()
        assert (row.version, row.epoch, row.buffer_lost_at) == ("0.1.0", "e1", None)
        assert row.last_heartbeat == datetime(2026, 11, 16, 19, 5, tzinfo=UTC)
        assert not [r for r in caplog.records if "buffer lost" in r.getMessage()]

        # the collector comes back with another epoch: its buffer (and seq counters) were lost
        await service.handle_batch([heartbeat("2026-11-16T19:10:00.000Z", "e2")])
        await service.handle_batch([heartbeat("2026-11-16T19:10:15.000Z", "e2")])  # the same epoch again: no new loss
    row = await collector()
    assert (row.epoch, row.buffer_lost_at) == ("e2", datetime(2026, 11, 16, 19, 10, tzinfo=UTC))
    lost = [r for r in caplog.records if r.getMessage() == "collector buffer lost, seq restarted"]
    assert len(lost) == 1 and lost[0].__dict__["collector_id"] == "c1"
