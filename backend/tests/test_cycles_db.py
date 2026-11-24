"""Cycles on a real PostgreSQL + TimescaleDB: built with the timeline, rebuilt with it, read through the API."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import identity
from app.api.main import app
from app.core.config import Settings
from app.timeline import service as timeline
from app.worker.__main__ import Worker, recalc
from app.worker.live import build_live
from tests.conftest import Seed, put_samples, seed_reference

Factory = async_sessionmaker[AsyncSession]
SETTINGS = Settings()
NOW = datetime.now(UTC).replace(microsecond=0)
T0 = NOW.replace(minute=0, second=0) - timedelta(hours=26)  # the history crosses a day boundary
PASSWORD = "secret-password"


def work(
    cycles: int, machining_s: int = 50, change_s: int = 20, start_s: int = 0, count: int = 0
) -> list[dict[str, Any]]:
    """Samples every 5 s: machining, then a part change; the counter grows when machining ends."""
    samples = []
    t = start_s
    for _ in range(cycles):
        for second in range(0, machining_s, 5):
            samples.append({"ts": T0 + timedelta(seconds=t + second), "run": "START", "part_count": count})
        t += machining_s
        count += 1
        for second in range(0, change_s, 5):
            samples.append({"ts": T0 + timedelta(seconds=t + second), "run": "STOP", "part_count": count})
        t += change_s
    return samples


async def stored(factory: Factory, machine_id: int) -> list[tuple[Any, ...]]:
    async with factory() as session:
        rows = await session.execute(
            text(
                "SELECT end_ts, start_ts, program, parts, machine_s, gap_s, kind FROM cycles "
                "WHERE machine_id = :m ORDER BY end_ts"
            ),
            {"m": machine_id},
        )
        return [tuple(r) for r in rows]


async def rebuild(factory: Factory, seed: Seed) -> None:
    worker = Worker(SETTINGS, factory)
    for _ in range(4):  # long histories are processed one day at a time
        await worker.rebuild_timeline(seed.machine, NOW)


@contextlib.asynccontextmanager
async def client(token: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1", headers=headers) as http:
        yield http


async def test_cycles_follow_the_timeline_and_its_recalculation(session_factory: Factory) -> None:
    seed = await seed_reference(session_factory)
    m = seed.machine_id
    # 20 cycles, a gap in the data, then 20 more cycles a day later
    await put_samples(session_factory, m, work(20, start_s=0) + work(20, start_s=90000, count=20))
    await rebuild(session_factory, seed)
    cycles = await stored(session_factory, m)
    assert len(cycles) == 39
    assert {(c[3], c[4], c[5]) for c in cycles if c[6] == "normal"} == {(1, 50.0, 20.0)}
    assert [c[6] for c in cycles].count("incomplete") == 1  # the cycle across the day without data

    # late data from an edge buffer: a part made in the middle of the gap
    late = [{"ts": T0 + timedelta(seconds=40000 + s), "run": "START", "part_count": 20} for s in range(0, 50, 5)] + [
        {"ts": T0 + timedelta(seconds=40050 + s), "run": "STOP", "part_count": 21} for s in range(0, 20, 5)
    ]
    await put_samples(session_factory, m, late)
    async with session_factory() as session, session.begin():  # the counter after it continues from 21
        await session.execute(
            text("UPDATE telemetry SET part_count = part_count + 1 WHERE machine_id = :m AND ts > :t"),
            {"m": m, "t": T0 + timedelta(seconds=40070)},
        )
        await timeline.mark_dirty(session, {m: T0})
    await rebuild(session_factory, seed)
    incremental = await stored(session_factory, m)
    assert len(incremental) == 40

    # a recalculation day by day from scratch gives exactly the same cycles
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM cycles WHERE machine_id = :m"), {"m": m})
    await recalc(session_factory, None, T0, NOW)
    assert await stored(session_factory, m) == incremental


async def test_cycle_stats_by_program_with_the_machine_threshold(session_factory: Factory) -> None:
    seed = await seed_reference(session_factory, norm_s=45.0)
    m = seed.machine_id
    # program O1: 10 cycles of 50 s + 20 s; the machine stands 30 minutes (connected) between two of them
    standing = [{"ts": T0 + timedelta(seconds=5 * 70 + s), "run": "STOP", "part_count": 5} for s in range(0, 1800, 5)]
    samples = work(5) + standing + work(5, start_s=5 * 70 + 1800, count=5)
    for sample in samples:
        sample["program"] = "O1"
    await put_samples(session_factory, m, samples)
    await rebuild(session_factory, seed)
    async with session_factory() as session, session.begin():
        await identity.bootstrap(session, SETTINGS)
        user = await identity.create_user(session, "engineer", PASSWORD, "Технолог")
        await identity.add_membership(session, user, "engineer", seed.org_id, seed.site_id, None)
        operator = await identity.create_user(session, "operator", PASSWORD, "Оператор")
        await identity.add_membership(session, operator, "operator", seed.org_id, seed.site_id, seed.area_id)
        token = await identity.login(session, SETTINGS, "engineer", PASSWORD)
        operator_token = await identity.login(session, SETTINGS, "operator", PASSWORD)
    app.state.settings, app.state.session_factory, app.state.redis = SETTINGS, session_factory, None
    assert token and operator_token
    period = {"from": (T0 - timedelta(hours=1)).isoformat(), "to": NOW.isoformat(), "machine_id": m}

    async with client(token) as http:
        (row,) = (await http.get("/cycles/stats", params=period)).json()
        assert (row["program"], row["total"], row["setup"], row["incomplete"]) == ("O1", 9, 0, 0)
        assert (row["machine_median_s"], row["load_median_s"], row["cycle_median_s"]) == (50.0, 20.0, 70.0)
        assert (row["threshold_s"], row["threshold_auto"]) == (600.0, True)  # max(3 x 50 s, 10 min)
        assert (row["pauses"], row["pause_s"]) == (1, 1820.0)  # the long gap is downtime, not part change
        assert row["interrupt_share"] == 0.0
        assert (row["norm_per_part_s"], row["machine_per_part_median_s"], row["vs_norm"]) == (45.0, 50.0, 1.111)
        assert sum(b["count"] for b in row["machine_hist"]) == 9 and sum(b["count"] for b in row["load_hist"]) == 8
        assert sum(day["cycles"] for day in row["by_day"]) == 9
        assert sum(shift["cycles"] for shift in row["by_shift"]) == 9
        assert (await http.get("/cycles/stats", params={**period, "program": "O9"})).json() == []

    # a threshold of the machine changes the split at once: no recalculation
    async with session_factory() as session, session.begin():
        await session.execute(text("UPDATE machines SET cycle_pause_threshold_s = 3600 WHERE id = :m"), {"m": m})
    async with client(token) as http:
        (row,) = (await http.get("/cycles/stats", params=period)).json()
        assert (row["threshold_s"], row["threshold_auto"], row["pauses"]) == (3600.0, False, 0)
        assert row["load_median_s"] == 20.0
    async with client(operator_token) as http:  # analytics is not for the operator
        assert (await http.get("/cycles/stats", params=period)).status_code == 403

    # the operator screen gets the last cycle and its norm through the live state
    async with session_factory() as session:
        live = await build_live(session, seed.machine, NOW)
    last = live["last_cycle"]
    assert (last["program"], last["kind"], last["duration_s"], last["machine_s"], last["norm_s"]) == (
        "O1",
        "normal",
        70.0,
        50.0,
        45.0,
    )


def test_histogram_bins_are_at_least_a_second_wide() -> None:
    from app.reports.cycles import histogram

    assert histogram([44.6, 44.9, 45.0, 45.0, 45.2]) == [
        {"from": 44.0, "to": 45.0, "count": 2},
        {"from": 45.0, "to": 46.0, "count": 3},
    ]
    bins = histogram([10, 20, 30, 70])
    assert (len(bins), bins[0]["from"], bins[-1]["to"], sum(b["count"] for b in bins)) == (12, 10.0, 70.0, 4)
    assert histogram([]) == [] and histogram([5, 5]) == [{"from": 5.0, "to": 6.0, "count": 2}]
