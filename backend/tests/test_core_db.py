"""Core hardening on a real PostgreSQL + TimescaleDB: restarts, closed recalculation, automatic marks."""

from __future__ import annotations

import itertools
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import journal
from app.api.main import app
from app.core.config import Settings
from app.core.security import login
from app.ingest.health import find_seq_gaps
from app.timeline import service as timeline
from app.worker.__main__ import Worker
from tests.conftest import DEFAULT_RULES, put_samples, seed_reference

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings()
NOW = datetime.now(UTC)
T0 = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def span(start: int, end: int, step: int = 10, **signals: Any) -> list[dict[str, Any]]:
    return [{"ts": at(t), **signals} for t in range(start, end, step)]


RUNNING = {"run": "START"}


async def rows(factory: Factory, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with factory() as session:
        return [tuple(r) for r in await session.execute(text(sql), params)]


async def intervals(factory: Factory) -> list[tuple[Any, ...]]:
    return await rows(factory, "SELECT state, start_ts, end_ts FROM state_intervals ORDER BY start_ts")


async def drain(worker: Worker, factory: Factory, machine: dict[str, Any], now: datetime = NOW) -> None:
    """Run worker steps until no recalculation mark is left."""
    for _ in range(100):
        await worker.rebuild_timeline(machine, now)
        await worker.update_oee(machine, now)
        if not await rows(factory, "SELECT 1 FROM timeline_dirty UNION ALL SELECT 1 FROM oee_dirty"):
            return
    raise AssertionError("recalculation marks never cleared")


async def test_worker_restart_between_timeline_and_oee_keeps_the_recalculation(session_factory: Factory) -> None:
    """1.1: the OEE mark lives in the database, not in worker memory."""
    machine = await seed_reference(session_factory)
    await put_samples(session_factory, "m1", span(0, 1800, **RUNNING))

    first = Worker(SETTINGS, session_factory)
    await first.rebuild_timeline(machine, NOW)  # ... and the worker dies before computing OEE
    assert await rows(session_factory, "SELECT machine_id, from_ts FROM oee_dirty") == [("m1", at(0))]
    assert await rows(session_factory, "SELECT 1 FROM oee_hourly WHERE period_start = :h", h=T0) == []

    restarted = Worker(SETTINGS, session_factory)  # a fresh process: nothing in memory
    await restarted.update_oee(machine, NOW)
    (run_time,) = (await rows(session_factory, "SELECT run_time_s FROM oee_hourly WHERE period_start = :h", h=T0))[0]
    assert run_time == pytest.approx(1820.0)  # 0..1790 s of samples, the last one trusted for 30 s
    assert await rows(session_factory, "SELECT 1 FROM oee_dirty") == []


HISTORY = [
    *span(0, 600, **RUNNING),
    *span(600, 1500),  # long stop: IDLE 120 s + STOP
    *span(1500, 2397, **RUNNING),
    {"ts": at(2397)},  # 3 s blip right at the recalculation border: debounced into RUN
    *span(2400, 3000, **RUNNING),
    *span(3000, 3300, alarm=True),
    *span(3300, 3600, **RUNNING),
]


async def test_closed_recalculation_is_stitched_and_repeatable(session_factory: Factory) -> None:
    """1.5: recalculating the middle of history keeps what follows it; a repeat changes nothing."""
    machine = await seed_reference(session_factory)
    await put_samples(session_factory, "m1", [{**s, "part_count": i // 30} for i, s in enumerate(HISTORY)])
    await drain(Worker(SETTINGS, session_factory), session_factory, machine)
    original = await intervals(session_factory)
    original_parts = await rows(session_factory, "SELECT ts, delta FROM part_events ORDER BY ts")
    assert [(s, (a - T0).total_seconds()) for s, a, _ in original] == [
        ("RUN", 0), ("IDLE", 600), ("STOP", 720), ("RUN", 1500), ("ALARM", 3000), ("RUN", 3300), ("OFFLINE", 3620),
    ]  # fmt: skip

    async def recalc(start: float, end: float) -> None:
        async with session_factory() as session, session.begin():
            await timeline.rebuild(session, "m1", NOW, force_from=at(start), force_to=at(end))

    # same data, same rules: borders inside RUN, inside STOP and exactly on the 3 s blip
    for start, end in [(300, 1000), (900, 2398), (1200, 2400), (0, 3600)]:
        await recalc(start, end)
        assert await intervals(session_factory) == original, (start, end)
        assert await rows(session_factory, "SELECT ts, delta FROM part_events ORDER BY ts") == original_parts

    # new rules for the middle only: STOP now starts after 60 s
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE state_rules SET rules = CAST(:r AS jsonb), version = 2 WHERE id = 1"),
            {"r": json.dumps({**DEFAULT_RULES, "idle_to_stop_s": 60})},
        )
    await recalc(300, 2400)
    changed = await intervals(session_factory)
    assert ("STOP", at(660), at(1500)) in changed
    after_border = [i for i in original if i[1] > at(2400)]
    assert [i for i in changed if i[1] > at(2400)] == after_border  # nothing after `to` moved
    assert all(a[2] == b[1] for a, b in itertools.pairwise(changed))  # the chain has no holes
    assert changed[-1][2] is None

    await recalc(300, 2400)
    assert await intervals(session_factory) == changed  # idempotent


async def test_long_period_is_rebuilt_in_daily_chunks(session_factory: Factory) -> None:
    """A mark far in the past is processed one day per step and gives the same timeline."""
    machine = await seed_reference(session_factory)
    start = T0 - timedelta(days=3)
    samples = [
        {"ts": start + timedelta(minutes=m), "run": "START" if (m // 60) % 3 else "STOP"} for m in range(0, 4000)
    ]
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE state_rules SET rules = CAST(:r AS jsonb) WHERE id = 1"),
            {"r": json.dumps({**DEFAULT_RULES, "offline_timeout_s": 120})},
        )
    await put_samples(session_factory, "m1", samples)

    worker = Worker(SETTINGS, session_factory)
    await worker.rebuild_timeline(machine, NOW)
    ((mark,),) = await rows(session_factory, "SELECT from_ts FROM timeline_dirty")
    assert mark == start + timedelta(days=1)  # first day done, the rest is still marked
    await drain(worker, session_factory, machine)
    chunked = await intervals(session_factory)

    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM state_intervals"))
        built = await _build_at_once(session, start)
    assert [(s, a) for s, a, _ in chunked] == built
    hours = await rows(session_factory, "SELECT count(*) FROM oee_hourly WHERE period_start >= :s", s=start)
    assert hours[0][0] >= 70  # OEE followed the timeline through all days


async def _build_at_once(session: AsyncSession, start: datetime) -> list[tuple[str, datetime]]:
    from app.timeline.builder import Rules, Sample, build_intervals

    samples = [
        Sample(ts=r.ts, mode=r.mode, run=r.run, alarm=r.alarm, emergency=r.emergency)
        for r in await session.execute(text("SELECT ts, mode, run, alarm, emergency FROM telemetry ORDER BY ts"))
    ]
    rules = Rules.from_config({**DEFAULT_RULES, "offline_timeout_s": 120})
    return [(i.state, i.start) for i in build_intervals(samples, rules, start, NOW)]


async def test_worker_never_writes_operator_input(session_factory: Factory) -> None:
    """1.7: the end of an open downtime reason is derived on read."""
    machine = await seed_reference(session_factory)
    await put_samples(session_factory, "m1", [*span(0, 600, **RUNNING), *span(600, 1800), *span(1800, 2400, **RUNNING)])
    async with session_factory() as session, session.begin():
        await journal.add_annotation(session, "m1", at(600), None, 30, None, None)  # planned: changeover
    await drain(Worker(SETTINGS, session_factory), session_factory, machine)

    stored = await rows(session_factory, "SELECT start_ts, end_ts FROM downtime_annotations")
    assert stored == [(at(600), None)]  # exactly what the operator entered
    async with session_factory() as session:
        (annotation,) = await journal.list_annotations(session, ["m1"], at(0), NOW)
        planned = await journal.planned_downtime(session, "m1", at(0), NOW, NOW)
    assert (annotation["end_ts"], annotation["open_ended"]) == (at(1800), True)
    assert planned == [(at(600), at(1800))]
    # planned downtime is excluded from planned production time of that hour
    ((planned_time, stop_s),) = await rows(
        session_factory, "SELECT planned_time_s, stop_s FROM oee_hourly WHERE period_start = :h", h=T0
    )
    assert (planned_time, stop_s) == (pytest.approx(2400.0), 0.0)

    # no code reachable from the worker modifies operator tables
    writes = re.compile(r"(UPDATE|DELETE FROM|INSERT INTO)\s+(downtime_annotations|scrap_records|operator_setup_marks)")
    app_dir = Path(__file__).resolve().parents[1] / "app"
    for module in ("worker", "timeline", "oee", "ingest", "reports"):
        for source in (app_dir / module).glob("*.py"):
            assert not writes.search(source.read_text()), source


@pytest.fixture
async def client(session_factory: Factory) -> Any:
    app.state.settings = SETTINGS
    app.state.session_factory = session_factory
    app.state.redis = None
    token = login(SETTINGS, SETTINGS.admin_username, SETTINGS.admin_password)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test/api/v1", headers={"Authorization": f"Bearer {token}"}
    ) as http:
        yield http


async def marks(factory: Factory) -> dict[str, list[tuple[Any, ...]]]:
    result = {
        "timeline": await rows(factory, "SELECT machine_id, from_ts FROM timeline_dirty ORDER BY 1"),
        "oee": await rows(factory, "SELECT machine_id, from_ts FROM oee_dirty ORDER BY 1"),
    }
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM timeline_dirty"))
        await session.execute(text("DELETE FROM oee_dirty"))
    return result


async def test_reference_changes_mark_recalculation(session_factory: Factory, client: httpx.AsyncClient) -> None:
    """1.6: changing shifts, norms, rules or reasons through the API leaves a mark; no manual recalc."""
    await seed_reference(session_factory)
    await marks(session_factory)
    horizon = datetime.now(UTC) - timedelta(days=SETTINGS.recalc_horizon_days)

    def near_horizon(found: list[tuple[Any, ...]]) -> bool:
        return [m for m, _ in found] == ["m1"] and abs((found[0][1] - horizon).total_seconds()) < 60

    # a new norm: valid from now by default, so only the present is recomputed
    created = await client.post("/admin/programs", json={"program": "O1", "ideal_cycle_time_s": 50})
    assert created.status_code == 201
    found = await marks(session_factory)
    assert found["timeline"] == [] and [m for m, _ in found["oee"]] == ["m1"]
    assert abs((found["oee"][0][1] - datetime.now(UTC)).total_seconds()) < 60

    # editing an old norm reaches back to its valid_from, limited by the horizon
    ((old_id,),) = await rows(session_factory, "SELECT id FROM programs ORDER BY valid_from LIMIT 1")
    updated = await client.put(f"/admin/programs/{old_id}", json={"program": "O1", "ideal_cycle_time_s": 44})
    assert updated.status_code == 200 and updated.json()["valid_from"].startswith("2000-01-01")
    assert near_horizon((await marks(session_factory))["oee"])

    ((shift_id,),) = await rows(session_factory, "SELECT id FROM shift_templates")
    body = {"site_id": "t1", "name": "Day", "start_time": "08:00:00", "end_time": "20:00:00"}
    assert (await client.put(f"/admin/shift-templates/{shift_id}", json=body)).status_code == 200
    assert near_horizon((await marks(session_factory))["oee"])

    reason = {"code": "CHANGEOVER", "name": "Changeover", "category": "unplanned", "parent_id": 3}
    assert (await client.put("/admin/downtime-reasons/30", json=reason)).status_code == 200
    assert near_horizon((await marks(session_factory))["oee"])
    reason["category"] = "planned"
    assert (await client.put("/admin/downtime-reasons/30", json=reason)).status_code == 200
    await marks(session_factory)

    rules = {"name": "Default", "rules": {**DEFAULT_RULES, "idle_to_stop_s": 90}, "version": 2}
    assert (await client.put("/admin/state-rules/1", json=rules)).status_code == 200
    found = await marks(session_factory)
    assert near_horizon(found["timeline"]) and found["oee"] == []  # OEE follows the rebuilt timeline

    # references that do not affect derived data leave no mark
    assert (await client.post("/admin/scrap-reasons", json={"code": "X1", "name": "Other"})).status_code == 201
    assert await marks(session_factory) == {"timeline": [], "oee": []}
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM scrap_reasons WHERE code = 'X1'"))


async def test_seq_gap_is_logged_and_shown_in_collector_state(
    session_factory: Factory, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """1.10: a hole in seq is visible in the worker log and in the collector state API."""
    await seed_reference(session_factory)
    async with session_factory() as session, session.begin():
        await session.execute(text("INSERT INTO collectors (id, site_id, name) VALUES ('c1', 't1', 'c1')"))
        await session.execute(text("UPDATE machines SET collector_id = 'c1'"))
    received = NOW - timedelta(minutes=5)
    samples = [{"ts": at(t), "seq": seq} for t, seq in [(0, 1), (10, 2), (20, 3), (50, 6), (60, 7)]]
    await put_samples(session_factory, "m1", samples, received_at=received)
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO machine_events (msg_id, ts, machine_id, collector_id, seq, type, payload, received_at) "
                "VALUES (gen_random_uuid(), :ts, 'm1', 'c1', 4, 'program_changed', '{}', :received)"
            ),
            {"ts": at(30), "received": received},
        )  # events share the counter with telemetry: seq 4 is not missing, 5 is

    async with session_factory() as session:
        (gap,) = await find_seq_gaps(session, NOW)
        assert await find_seq_gaps(session, received + timedelta(seconds=10)) == []  # still within the grace time
    assert (gap["machine_id"], gap["after_seq"], gap["before_seq"], gap["missing"]) == ("m1", 4, 6, 1)

    with caplog.at_level(logging.WARNING, logger="app.worker"):
        worker = Worker(SETTINGS, session_factory)
        await worker.check_seq_gaps(datetime.now(UTC))
        await worker.check_seq_gaps(datetime.now(UTC) + timedelta(minutes=5))  # the same gap is logged once
    logged = [r for r in caplog.records if r.getMessage() == "seq gap: messages lost"]
    assert len(logged) == 1 and logged[0].__dict__["missing"] == 1

    (collector,) = (await client.get("/collectors")).json()
    assert (collector["id"], collector["online"], collector["missing_messages"]) == ("c1", False, 1)
    assert collector["seq_gaps"][0]["after_seq"] == 4

    # the late message arrives: the gap disappears by itself
    await put_samples(session_factory, "m1", [{"ts": at(40), "seq": 5}], received_at=received)
    assert (await client.get("/collectors")).json()[0]["seq_gaps"] == []
