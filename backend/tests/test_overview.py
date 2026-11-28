"""Overview of a site: utilization with RUN outside shifts, comparison with the previous period and the
usual level, "no data to compare", the thresholds of the lagging lists."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin import service as admin
from app.oee import service as oee
from app.reports import overview as report
from app.timeline import service as timeline
from tests.conftest import Seed, put_samples, seed_reference

Factory = async_sessionmaker[AsyncSession]
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)  # Wednesday noon, the site is in UTC
MIDNIGHT = NOW.replace(hour=0)
HISTORY_DAYS = 29  # four reference weeks for today, not enough for the period before the last 30 days
NORM_S = 400.0


def day_samples(
    day: datetime, start_h: int, end_h: int, cycle_s: float, count: int
) -> tuple[list[dict[str, Any]], int]:
    """Stopped all day except [start_h, end_h): running, a part every ``cycle_s`` seconds."""
    samples = [{"ts": day, "run": "STOP", "part_count": count}]
    t = day + timedelta(hours=start_h)
    samples.append({"ts": t, "run": "START", "part_count": count})
    while t + timedelta(seconds=cycle_s) < day + timedelta(hours=end_h):
        t += timedelta(seconds=cycle_s)
        count += 1
        samples.append({"ts": t, "run": "START", "part_count": count})
    samples.append({"ts": day + timedelta(hours=end_h), "run": "STOP", "part_count": count})
    return [s for s in samples if s["ts"] <= NOW], count


async def history(factory: Factory, machine_id: int, collector: str, cycle_s: float, today_start_h: int = 6) -> None:
    """29 days and this morning: running 06:00-18:00 every day (this day from ``today_start_h``)."""
    samples: list[dict[str, Any]] = []
    count = 0
    for d in range(HISTORY_DAYS, -1, -1):
        day = MIDNIGHT - timedelta(days=d)
        chunk, count = day_samples(day, today_start_h if d == 0 else 6, 18, cycle_s, count)
        samples += chunk
    await put_samples(factory, machine_id, samples, collector=collector)


async def recalc(factory: Factory, machine: dict[str, Any]) -> None:
    cursor = MIDNIGHT - timedelta(days=HISTORY_DAYS)
    while cursor < NOW:
        chunk_end = min(cursor + timedelta(days=1), NOW)
        async with factory() as session, session.begin():
            await timeline.rebuild(session, machine["id"], NOW, force_from=cursor, force_to=chunk_end)
            await oee.recompute(session, machine, cursor, chunk_end, NOW)
        cursor = chunk_end


async def add_machine(factory: Factory, seed: Seed, code: str) -> dict[str, Any]:
    async with factory() as session, session.begin():
        machine_id = (
            await session.execute(
                text(
                    "INSERT INTO machines (site_id, area_id, code, name, collector_id) "
                    "VALUES (:s, :a, :c, :c, :col) RETURNING id"
                ),
                {"s": seed.site_id, "a": seed.area_id, "c": code, "col": seed.machine["collector_id"]},
            )
        ).scalar_one()
    async with factory() as session:
        (row,) = await admin.list_machines(session, [machine_id])
    return row


@pytest.fixture
async def site(session_factory: Factory) -> tuple[Seed, list[dict[str, Any]]]:
    """m1 started late today; m2 runs as usual; m3 runs as usual but 25 % slower than the cycle norm.

    One day shift 08:00-16:00: the machines also run two hours before and after it.
    """
    seed = await seed_reference(session_factory, norm_s=NORM_S)
    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE shift_templates SET name = 'Day', start_time = '08:00', end_time = '16:00' WHERE org_id = :o"),
            {"o": seed.org_id},
        )
        # samples only at changes: nothing turns into OFFLINE between them
        await session.execute(
            text(
                "UPDATE state_rules SET rules = jsonb_set(rules, '{offline_timeout_s}', '100000000') WHERE org_id = :o"
            ),
            {"o": seed.org_id},
        )
    m1 = seed.machine
    m2 = await add_machine(session_factory, seed, "m2")
    m3 = await add_machine(session_factory, seed, "m3")
    collector = m1["collector_id"]
    await history(session_factory, m1["id"], collector, NORM_S, today_start_h=8)
    await history(session_factory, m2["id"], collector, NORM_S)
    await history(session_factory, m3["id"], collector, NORM_S * 1.25)
    for machine in (m1, m2, m3):
        await recalc(session_factory, machine)
    return seed, [m1, m2, m3]


async def run(
    factory: Factory, site: tuple[Seed, list[dict[str, Any]]], period: str, drop_pp: float = 10, slow_pct: float = 10
) -> dict[str, Any]:
    seed, machines = site
    node = {"org_id": seed.org_id, "site_id": seed.site_id, "timezone": "UTC"}
    async with factory() as session:
        return await report.overview(session, node, machines, period, NOW, drop_pp, slow_pct)


def by_name(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["name"]: m for m in result["machines"]}


async def test_utilization_counts_run_outside_shifts_and_oee_only_shift_hours(
    session_factory: Factory, site: tuple[Seed, list[dict[str, Any]]]
) -> None:
    result = await run(session_factory, site, "yesterday")
    assert result["period"]["step"] == "hour" and len(result["buckets"]) == 24
    machines = by_name(result)
    # 12 hours of RUN in 24 calendar hours, though the shift is only 8 of them
    assert machines["m2"]["utilization"]["value"] == pytest.approx(0.5)
    cards = result["cards"]
    assert cards["availability"]["value"] == pytest.approx(1.0)  # in the shift the machines always ran
    # 07:00 is outside the shift: running, so utilization is 100 %, and OEE is a gap, not a zero
    assert cards["utilization"]["series"][7] == pytest.approx(1.0)
    assert cards["oee"]["series"][7] is None and cards["availability"]["series"][3] is None
    assert cards["utilization"]["series"][3] == 0.0
    assert cards["availability"]["series"][9] == pytest.approx(1.0)


async def test_change_to_the_previous_period_and_the_usual_level(
    session_factory: Factory, site: tuple[Seed, list[dict[str, Any]]]
) -> None:
    result = await run(session_factory, site, "today")
    card = result["cards"]["utilization"]
    # this morning: m1 4 h, m2 and m3 6 h of RUN in 12 h; yesterday morning 6 h each
    assert card["value"] == pytest.approx(16 / 36)
    assert card["previous"] == pytest.approx(0.5)
    assert card["change"] == pytest.approx(16 / 36 - 0.5)
    assert result["period"]["comparable"] is True
    # the same hour on the 4 previous Wednesdays: every machine was running at 07:00
    assert card["usual_series"][7] == pytest.approx(1.0)
    assert card["series"][7] == pytest.approx(2 / 3)
    assert card["series"][13] is None  # the future has no value yet
    assert card["previous_series"][13] == pytest.approx(1.0) and card["usual_series"][13] == pytest.approx(1.0)
    m1 = by_name(result)["torg m1"]
    assert m1["utilization"]["usual"] == pytest.approx(0.5)
    assert m1["parts"]["value"] < m1["parts"]["previous"]
    assert result["output"]["previous"] is not None and result["output"]["total"] > 0

    week = await run(session_factory, site, "7d")
    assert week["period"]["step"] == "day" and len(week["buckets"]) == 7
    assert week["cards"]["utilization"]["previous"] is not None


async def test_no_data_to_compare_before_the_history(
    session_factory: Factory, site: tuple[Seed, list[dict[str, Any]]]
) -> None:
    result = await run(session_factory, site, "30d")
    assert len(result["buckets"]) == 30
    assert result["period"]["comparable"] is False
    for name in report.METRICS:
        card = result["cards"][name]
        assert card["previous"] is None and card["change"] is None, name
        assert set(card["previous_series"]) == {None}, name
    assert result["output"]["previous"] is None and result["output"]["change"] is None
    usual = result["cards"]["utilization"]["usual_series"]
    assert usual[0] is None  # four weeks before the first day there was nothing yet
    assert usual[-1] is not None
    assert all(m["utilization"]["previous"] is None for m in result["machines"])


async def test_below_usual_and_slower_than_norm_follow_their_thresholds(
    session_factory: Factory, site: tuple[Seed, list[dict[str, Any]]]
) -> None:
    result = await run(session_factory, site, "today")
    (below,) = result["below_usual"]
    assert below["name"] == "torg m1"
    assert below["change"] == pytest.approx(4 / 12 - 0.5)
    (slow,) = result["slow_cycles"]
    assert (slow["name"], slow["program"]) == ("m3", "O1")
    assert slow["norm_s"] == NORM_S and slow["median_s"] == pytest.approx(NORM_S * 1.25)
    assert slow["diff_pct"] == pytest.approx(25.0)

    relaxed = await run(session_factory, site, "today", drop_pp=20, slow_pct=30)
    assert relaxed["below_usual"] == [] and relaxed["slow_cycles"] == []


# the query state_seconds used before: reads the whole history of a machine up to the end of each span
_REFERENCE = """
SELECT p.n, i.machine_id, i.state, sum(EXTRACT(EPOCH FROM LEAST(COALESCE(i.end_ts, :now), p.end_ts)
       - GREATEST(i.start_ts, p.start_ts))) AS seconds
FROM unnest(CAST(:n AS integer[]), CAST(:starts AS timestamptz[]), CAST(:ends AS timestamptz[]))
     AS p(n, start_ts, end_ts)
JOIN state_intervals i ON i.machine_id = ANY(:ids) AND i.start_ts < p.end_ts AND COALESCE(i.end_ts, :now) > p.start_ts
GROUP BY p.n, i.machine_id, i.state
"""


async def test_state_seconds_reads_only_what_covers_the_span(session_factory: Factory) -> None:
    """Intervals starting inside a span plus the last one before it give what the full scan gives:
    an open interval, an interval longer than the span, a span before the history, a span after it."""
    seed = await seed_reference(session_factory)
    other = await add_machine(session_factory, seed, "m2")
    h = timedelta(hours=1)
    t0 = NOW - 48 * h
    chains = {
        seed.machine_id: [
            ("STOP", t0, t0 + 2 * h),
            ("RUN", t0 + 2 * h, t0 + 30 * h),
            ("IDLE", t0 + 30 * h, t0 + 31 * h),
        ]
        + [("RUN" if k % 2 else "STOP", t0 + (31 + k) * h, t0 + (32 + k) * h) for k in range(15)]
        + [("RUN", t0 + 46 * h, None)],  # open until now
        other["id"]: [("OFFLINE", t0 + 10 * h, t0 + 11 * h), ("RUN", t0 + 11 * h, t0 + 12 * h)],  # history ends early
    }
    async with session_factory() as session, session.begin():
        for machine_id, chain in chains.items():
            for state, start, end in chain:
                await session.execute(
                    text(
                        "INSERT INTO state_intervals (machine_id, state, start_ts, end_ts, rules_version) "
                        "VALUES (:m, :s, :a, :b, 1)"
                    ),
                    {"m": machine_id, "s": state, "a": start, "b": end},
                )
    spans = [
        (t0 - 5 * h, t0 - 2 * h),  # before any history
        (t0 + 5 * h, t0 + 6 * h),  # inside one interval that is much longer than the span
        (t0 + h, t0 + 33 * h + timedelta(minutes=17)),  # across many intervals
        (t0 + 31 * h + timedelta(minutes=30), t0 + 32 * h + timedelta(minutes=30)),  # across a border
        (t0 + 46 * h + timedelta(minutes=5), NOW),  # inside the open interval
        (NOW - 3 * h, NOW + 2 * h),  # reaches into the future
        *[(t0 + k * h, t0 + (k + 1) * h) for k in range(48)],  # every hour
    ]
    ids = list(chains)
    async with session_factory() as session:
        found = await timeline.state_seconds(session, ids, spans, NOW)
        rows = await session.execute(
            text(_REFERENCE),
            {
                "n": list(range(len(spans))),
                "starts": [s for s, _ in spans],
                "ends": [e for _, e in spans],
                "ids": ids,
                "now": NOW,
            },
        )
    expected: dict[tuple[int, int], dict[str, float]] = {}
    for r in rows:
        expected.setdefault((r.n, r.machine_id), {})[r.state] = max(float(r.seconds), 0.0)
    assert found == expected
    assert (0, seed.machine_id) not in found and (1, seed.machine_id) in found
    assert found[(1, seed.machine_id)] == {"RUN": 3600.0}
    assert found[(4, seed.machine_id)]["RUN"] == pytest.approx(
        (NOW - (t0 + 46 * h + timedelta(minutes=5))).total_seconds()
    )
    assert (4, other["id"]) not in found  # after the end of its history: nothing, not an error
