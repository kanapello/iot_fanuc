"""Overview of an enterprise site: utilization, OEE and its parts, output, downtime, lagging machines.

Everything is read from derived data, never from raw telemetry:

* utilization = RUN time / calendar time from ``state_intervals``, so RUN outside shifts and breaks counts;
* OEE, availability and performance from the summed components of ``oee_hourly`` (hours outside shifts
  have no rows: a gap on the chart, not a zero);
* output from ``part_events`` and ``scrap_records``; downtime from ``downtime_pareto``; cycles from
  ``cycles.summaries``.

A period is compared with the previous period of the same kind (the same elapsed part of it) and with
the usual level: for each step the same step on the last 4 same weekdays. Spans before the history of
the machines are not data: such comparisons are ``None`` ("no data to compare"), never zeros.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import journal
from app.oee import service as oee
from app.oee.calc import aggregate
from app.reports import alarms
from app.reports import cycles as cycle_reports
from app.reports.service import downtime_pareto
from app.timeline import service as timeline
from app.timeline.builder import ALARM, IDLE, OFFLINE, RUN, SETUP, STOP

PERIODS = ("shift", "today", "yesterday", "7d", "30d")
REFERENCE_WEEKS = 4  # the usual level: the same step on the last 4 same weekdays
DOWNTIME_STATES = (IDLE, STOP, ALARM, SETUP)  # not running while connected
TOP_REASONS = 5
METRICS = ("utilization", "oee", "performance", "availability")

Span = tuple[datetime, datetime]


@dataclass(slots=True)
class Bucket:
    start: datetime
    end: datetime
    label: str


@dataclass(slots=True)
class Period:
    key: str
    step: str  # hour | day
    start: datetime
    end: datetime
    buckets: list[Bucket]
    previous: Callable[[datetime], datetime]  # moves a moment of this period into the previous one
    shift_name: str | None = None


def _wall(tz: ZoneInfo, days: int) -> Callable[[datetime], datetime]:
    """Moves a moment by whole days of the site's calendar (the same wall-clock time)."""
    return lambda moment: (moment.astimezone(tz) + timedelta(days=days)).astimezone(UTC)


def _steps(start: datetime, end: datetime, tz: ZoneInfo, step: str) -> list[Bucket]:
    buckets = []
    current = start
    while current < end:
        if step == "hour":
            following = min(current + timedelta(hours=1), end)
            label = current.astimezone(tz).strftime("%H:%M")
        else:
            following = min(_wall(tz, 1)(current), end)
            label = current.astimezone(tz).strftime("%d.%m")
        buckets.append(Bucket(current, following, label))
        current = following
    return buckets


async def resolve_period(session: AsyncSession, node: dict[str, Any], key: str, now: datetime) -> Period:
    """Boundaries of a period in the time zone of the site; ``node``: org_id, site_id, timezone."""
    tz = ZoneInfo(node["timezone"])
    midnight = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tzinfo=tz).astimezone(UTC)
    if key in ("today", "yesterday"):
        start = midnight if key == "today" else _wall(tz, -1)(midnight)
        end = _wall(tz, 1)(start)
        return Period(key, "hour", start, end, _steps(start, end, tz, "hour"), _wall(tz, -1))
    if key in ("7d", "30d"):
        days = 7 if key == "7d" else 30
        start, end = _wall(tz, -(days - 1))(midnight), _wall(tz, 1)(midnight)
        return Period(key, "day", start, end, _steps(start, end, tz, "day"), _wall(tz, -days))
    if key != "shift":
        raise ValueError(f"unknown period {key!r}")
    # the shift running now, else the last one that started; the previous one is the same shift before it
    shifts = await oee.site_shifts(session, node, now - timedelta(days=9), now + timedelta(seconds=1))
    started = [s for s in shifts if s.start <= now]
    if not started:
        return Period(key, "hour", now, now, [], lambda moment: moment)
    current = next((s for s in reversed(started) if s.end > now), started[-1])
    before = [s for s in started if s.name == current.name and s.start < current.start]
    offset = (before[-1].start - current.start) if before else -timedelta(days=1)
    return Period(
        key,
        "hour",
        current.start,
        current.end,
        _steps(current.start, current.end, tz, "hour"),
        lambda moment: moment + offset,
        current.name,
    )


class _Spans:
    """Every span the overview reads, each once: one query per data source."""

    def __init__(self) -> None:
        self.items: list[Span] = []
        self._index: dict[Span, int] = {}

    def add(self, span: Span | None) -> int | None:
        if span is None or span[1] <= span[0]:
            return None
        if span not in self._index:
            self._index[span] = len(self.items)
            self.items.append(span)
        return self._index[span]


@dataclass(slots=True)
class _Data:
    spans: list[Span]
    states: dict[tuple[int, int], dict[str, float]]
    components: dict[tuple[int, int], dict[str, Any]]
    parts: dict[tuple[int, int], int]
    history: dict[int, datetime]  # where the timeline of each machine begins
    now: datetime

    def covered(self, n: int | None, machine_ids: Iterable[int]) -> bool:
        """The span lies inside the history of at least one of the machines."""
        if n is None:
            return False
        starts = [self.history[m] for m in machine_ids if m in self.history]
        return bool(starts) and self.spans[n][0] >= min(starts)

    def calendar(self, n: int, machine_id: int) -> float:
        start, end = self.spans[n]
        first = self.history.get(machine_id)
        if first is None:
            return 0.0
        lo, hi = max(start, first), min(end, self.now)
        return max((hi - lo).total_seconds(), 0.0)

    def metrics(self, spans: Iterable[int | None], machine_ids: Sequence[int]) -> dict[str, float | None]:
        """Utilization and OEE of the machines over the spans, from summed components."""
        run = calendar = 0.0
        rows = []
        for n in spans:
            if n is None:
                continue
            for m in machine_ids:
                run += self.states.get((n, m), {}).get(RUN, 0.0)
                calendar += self.calendar(n, m)
                if (n, m) in self.components:
                    rows.append(self.components[(n, m)])
        total = aggregate(rows) if rows else None
        return {
            "utilization": run / calendar if calendar > 0 else None,
            "oee": total["oee"] if total else None,
            "performance": total["performance"] if total else None,
            "availability": total["availability"] if total else None,
        }

    def state_sum(self, spans: Iterable[int | None], machine_ids: Sequence[int], states: Sequence[str]) -> float:
        return sum(
            self.states.get((n, m), {}).get(state, 0.0)
            for n in spans
            if n is not None
            for m in machine_ids
            for state in states
        )

    def parts_sum(self, spans: Iterable[int | None], machine_ids: Sequence[int]) -> int:
        return sum(self.parts.get((n, m), 0) for n in spans if n is not None for m in machine_ids)


@dataclass(slots=True)
class _Layout:
    """Indexes of the spans of every bucket: elapsed, previous (whole and elapsed), reference weeks."""

    elapsed: list[int | None] = field(default_factory=list)
    previous: list[int | None] = field(default_factory=list)
    previous_elapsed: list[int | None] = field(default_factory=list)
    reference: list[list[int | None]] = field(default_factory=list)  # [bucket][week]
    reference_elapsed: list[list[int | None]] = field(default_factory=list)


def _layout(period: Period, tz: ZoneInfo, now: datetime, spans: _Spans) -> _Layout:
    layout = _Layout()
    weeks = [_wall(tz, -7 * k) for k in range(1, REFERENCE_WEEKS + 1)]
    for b in period.buckets:
        elapsed = (b.start, min(b.end, now)) if b.start < now else None
        move = period.previous
        layout.elapsed.append(spans.add(elapsed))
        layout.previous.append(spans.add((move(b.start), move(b.end))))
        layout.previous_elapsed.append(spans.add((move(elapsed[0]), move(elapsed[1])) if elapsed else None))
        layout.reference.append([spans.add((w(b.start), w(b.end))) for w in weeks])
        layout.reference_elapsed.append([spans.add((w(elapsed[0]), w(elapsed[1])) if elapsed else None) for w in weeks])
    return layout


def _change(current: float | None, previous: float | None) -> float | None:
    return None if current is None or previous is None else current - previous


async def overview(
    session: AsyncSession,
    node: dict[str, Any],
    machines: Sequence[dict[str, Any]],
    period_key: str,
    now: datetime,
    utilization_drop_pp: float,
    slow_cycle_pct: float,
) -> dict[str, Any]:
    """The overview of ``machines`` (one site, ``node``) for a period ending at ``now`` at the latest."""
    tz = ZoneInfo(node["timezone"])
    period = await resolve_period(session, node, period_key, now)
    ids = [m["id"] for m in machines]
    spans = _Spans()
    layout = _layout(period, tz, now, spans)
    data = _Data(
        spans.items,
        await timeline.state_seconds(session, ids, spans.items, now),
        await oee.component_sums(session, ids, spans.items),
        await timeline.part_totals(session, ids, spans.items),
        await timeline.first_interval_at(session, ids),
        now,
    )

    elapsed = [n for n in layout.elapsed if n is not None]
    started = period.start < now and bool(elapsed)
    end_now = min(period.end, now)
    prev_start, prev_end = period.previous(period.start), period.previous(end_now)

    def comparable(span_ids: Sequence[int | None], machine_ids: Sequence[int]) -> bool:
        """The whole previous elapsed period lies inside the history: otherwise it is not compared."""
        present = [n for n in span_ids if n is not None]
        return bool(present) and all(data.covered(n, machine_ids) for n in present)

    prev_ok = started and comparable([layout.previous_elapsed[0]], ids)

    # ---- cards
    current_value = data.metrics(elapsed, ids)
    previous_value = data.metrics(layout.previous_elapsed, ids) if prev_ok else dict.fromkeys(METRICS)
    series: dict[str, dict[str, list[float | None]]] = {
        name: {"series": [], "previous_series": [], "usual_series": []} for name in METRICS
    }
    for i in range(len(period.buckets)):
        cur = data.metrics([layout.elapsed[i]], ids) if layout.elapsed[i] is not None else dict.fromkeys(METRICS)
        prev_n = layout.previous[i]
        prev = data.metrics([prev_n], ids) if data.covered(prev_n, ids) else dict.fromkeys(METRICS)
        refs = [n for n in layout.reference[i] if data.covered(n, ids)]
        usual = data.metrics(refs, ids) if refs else dict.fromkeys(METRICS)
        for name in METRICS:
            series[name]["series"].append(cur[name])
            series[name]["previous_series"].append(prev[name])
            series[name]["usual_series"].append(usual[name])
    cards = {
        name: {
            "value": current_value[name],
            "previous": previous_value[name],
            "change": _change(current_value[name], previous_value[name]),
            **series[name],
        }
        for name in METRICS
    }

    # ---- output: parts of the counters and scrap as entered
    scrap_now = await journal.list_scrap(session, ids, period.start, end_now) if started else []
    scrap_prev = await journal.list_scrap(session, ids, prev_start, prev_end) if prev_ok else []
    parts_now = data.parts_sum(elapsed, ids)
    scrapped = sum(r["qty"] for r in scrap_now)
    output: dict[str, Any] = {"total": parts_now, "good": max(parts_now - scrapped, 0), "scrap": scrapped}
    if prev_ok:
        parts_prev = data.parts_sum(layout.previous_elapsed, ids)
        prev_scrap = sum(r["qty"] for r in scrap_prev)
        output["previous"] = {"total": parts_prev, "good": max(parts_prev - prev_scrap, 0), "scrap": prev_scrap}
        output["change"] = (parts_now - parts_prev) / parts_prev if parts_prev > 0 else None
    else:
        output["previous"] = None
        output["change"] = None

    # ---- downtime by reason: the top reasons plus time without a reason, by state
    pareto = await downtime_pareto(session, ids, period.start, end_now, now) if started else []
    reasons = [r for r in pareto if r["reason_id"] is not None]
    unexplained = {r["state"]: r["duration_s"] for r in pareto if r["reason_id"] is None}
    downtime = {
        "reasons": [
            {
                "reason_id": r["reason_id"],
                "name": r["name"],
                "group_name": r["group_name"],
                "category": r["category"],
                "color": r["color"],
                "duration_s": r["duration_s"],
                "count": r["count"],
            }
            for r in reasons[:TOP_REASONS]
        ],
        "other_reasons_s": sum(r["duration_s"] for r in reasons[TOP_REASONS:]),
        "unexplained": {
            "total_s": sum(unexplained.values()),
            "by_state": {state: unexplained.get(state, 0.0) for state in (*DOWNTIME_STATES, OFFLINE)},
        },
    }

    # ---- per machine: the side panel and the "below usual" list
    episodes_now = await alarms.episodes(session, ids, period.start, end_now, now) if started else []
    episodes_prev = await alarms.episodes(session, ids, prev_start, prev_end, now) if prev_ok else []
    alarms_now = Counter(e["machine_id"] for e in episodes_now)
    alarms_prev = Counter(e["machine_id"] for e in episodes_prev)
    per_machine = []
    below = []
    for machine in machines:
        mid = [machine["id"]]
        own_prev = started and comparable([layout.previous_elapsed[0]], mid)
        cur = data.metrics(elapsed, mid)
        prev = data.metrics(layout.previous_elapsed, mid) if own_prev else dict.fromkeys(METRICS)
        refs = [n for row in layout.reference_elapsed for n in row if data.covered(n, mid)]
        usual_level = data.metrics(refs, mid)["utilization"] if refs else None

        def pair(current: float | None, previous: float | None, ok: bool = own_prev) -> dict[str, Any]:
            return {"value": current, "previous": previous if ok else None}

        per_machine.append(
            {
                "machine_id": machine["id"],
                "name": machine["name"],
                "area_id": machine["area_id"],
                "area_name": machine["area_name"],
                "utilization": {**pair(cur["utilization"], prev["utilization"]), "usual": usual_level},
                "oee": pair(cur["oee"], prev["oee"]),
                "parts": pair(data.parts_sum(elapsed, mid), data.parts_sum(layout.previous_elapsed, mid)),
                "downtime_s": pair(
                    data.state_sum(elapsed, mid, DOWNTIME_STATES),
                    data.state_sum(layout.previous_elapsed, mid, DOWNTIME_STATES),
                ),
                "alarms": pair(alarms_now[machine["id"]], alarms_prev[machine["id"]]),
            }
        )
        utilization = cur["utilization"]
        if (
            utilization is not None
            and usual_level is not None
            and (usual_level - utilization) * 100 > utilization_drop_pp
        ):
            below.append(
                {
                    "machine_id": machine["id"],
                    "name": machine["name"],
                    "utilization": utilization,
                    "usual": usual_level,
                    "change": utilization - usual_level,
                }
            )
    below.sort(key=lambda r: r["change"])

    # ---- cycles slower than the norm: median machine time per part against the norm per part
    slow = []
    if started:
        for row in await cycle_reports.summaries(session, machines, period.start, end_now):
            fact, norm = row["machine_per_part_median_s"], row["norm_per_part_s"]
            if fact is None or not norm or fact <= norm * (1 + slow_cycle_pct / 100):
                continue
            slow.append(
                {
                    "machine_id": row["machine_id"],
                    "name": row["machine_name"],
                    "program": row["program"],
                    "cycles": row["cycles"],
                    "median_s": fact,
                    "norm_s": norm,
                    "diff_s": round(fact - norm, 1),
                    "diff_pct": round((fact / norm - 1) * 100, 1),
                }
            )
    slow.sort(key=lambda r: -r["diff_pct"])

    return {
        "period": {
            "key": period.key,
            "step": period.step,
            "from": period.start,
            "to": period.end,
            "now": now,
            "previous_from": prev_start,
            "previous_to": prev_end,
            "comparable": prev_ok,
            "shift_name": period.shift_name,
            "timezone": node["timezone"],
            "site_id": node["site_id"],
        },
        "buckets": [{"from": b.start, "to": b.end, "label": b.label} for b in period.buckets],
        "cards": cards,
        "output": output,
        "downtime": downtime,
        "below_usual": below,
        "slow_cycles": slow,
        "machines": per_machine,
        "settings": {"utilization_drop_pp": utilization_drop_pp, "slow_cycle_pct": slow_cycle_pct},
    }
