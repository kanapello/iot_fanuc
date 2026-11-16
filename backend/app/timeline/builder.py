"""Pure functions: telemetry samples -> state intervals and part events.

States follow ARCHITECTURE.md 5.4. Nothing here touches the database, which
keeps the rules testable with golden scenarios.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

RUN, IDLE, STOP, ALARM, SETUP, OFFLINE = "RUN", "IDLE", "STOP", "ALARM", "SETUP", "OFFLINE"
NOT_RUN = "NOT_RUN"  # internal: becomes IDLE (first minutes) and STOP (the rest)
STATES = (RUN, IDLE, STOP, ALARM, SETUP, OFFLINE)


@dataclass(frozen=True, slots=True)
class Rules:
    offline_timeout_s: float = 30.0
    idle_to_stop_s: float = 120.0
    min_interval_s: float = 5.0
    run_modes: tuple[str, ...] = ("MEM", "RMT", "DNC")
    setup_modes: tuple[str, ...] = ("EDIT", "MDI", "JOG", "HND")
    version: int = 1

    @classmethod
    def from_config(cls, config: dict[str, Any] | None, version: int = 1) -> Rules:
        config = config or {}
        defaults = cls()
        return cls(
            offline_timeout_s=float(config.get("offline_timeout_s", defaults.offline_timeout_s)),
            idle_to_stop_s=float(config.get("idle_to_stop_s", defaults.idle_to_stop_s)),
            min_interval_s=float(config.get("min_interval_s", defaults.min_interval_s)),
            run_modes=tuple(config.get("run_modes", defaults.run_modes)),
            setup_modes=tuple(config.get("setup_modes", defaults.setup_modes)),
            version=version,
        )


@dataclass(frozen=True, slots=True)
class Sample:
    ts: datetime
    connected: bool = True
    mode: str | None = None
    run: str | None = None
    alarm: bool | None = False
    emergency: bool | None = False
    program: str | None = None
    part_count: int | None = None


@dataclass(frozen=True, slots=True)
class Interval:
    state: str
    start: datetime
    end: datetime | None  # None = still open
    program: str | None = None


@dataclass(frozen=True, slots=True)
class PartEvent:
    ts: datetime
    program: str | None
    delta: int


@dataclass(slots=True)
class _Segment:
    state: str
    start: datetime
    end: datetime
    program: str | None = field(default=None)


def classify(sample: Sample, rules: Rules, setup_marked: bool) -> str:
    """State of one sample; rules are checked top-down, first match wins."""
    if not sample.connected:
        return OFFLINE
    if sample.alarm or sample.emergency:
        return ALARM
    if setup_marked:
        return SETUP
    if sample.run == "START" and sample.mode in rules.run_modes:
        return RUN
    if sample.mode in rules.setup_modes:
        return SETUP
    return NOT_RUN


def _marked(marks: Sequence[tuple[datetime, datetime | None]], at: datetime) -> bool:
    return any(start <= at and (end is None or at < end) for start, end in marks)


def _base_segments(
    points: Sequence[Sample],
    rules: Rules,
    start: datetime,
    now: datetime,
    marks: Sequence[tuple[datetime, datetime | None]],
) -> list[_Segment]:
    """Each sample holds its state until the next one, but not longer than offline_timeout."""
    segments: list[_Segment] = []

    def add(state: str, seg_start: datetime, seg_end: datetime, program: str | None) -> None:
        seg_start, seg_end = max(seg_start, start), min(seg_end, now)
        if seg_end > seg_start:
            segments.append(_Segment(state, seg_start, seg_end, program))

    if not points:
        add(OFFLINE, start, now, None)
        return segments
    add(OFFLINE, start, points[0].ts, None)
    timeout = timedelta(seconds=rules.offline_timeout_s)
    for i, point in enumerate(points):
        next_ts = points[i + 1].ts if i + 1 < len(points) else now
        hold_end = min(next_ts, point.ts + timeout)
        # operator setup marks may begin or end while the sample is held
        cuts = sorted({t for mark in marks for t in mark if t is not None and point.ts < t < hold_end})
        edges = [point.ts, *cuts, hold_end]
        for seg_start, seg_end in itertools.pairwise(edges):
            add(classify(point, rules, _marked(marks, seg_start)), seg_start, seg_end, point.program)
        add(OFFLINE, hold_end, next_ts, point.program)
    return segments


def _merge(segments: list[_Segment]) -> list[_Segment]:
    merged: list[_Segment] = []
    for seg in segments:
        if merged and merged[-1].state == seg.state:
            merged[-1].end = seg.end
        else:
            merged.append(replace(seg))
    return merged


def _merge_and_debounce(segments: list[_Segment], min_interval_s: float) -> list[_Segment]:
    """Join equal neighbours; glue intervals shorter than min_interval to the previous one."""
    segments = _merge(segments)
    out: list[_Segment] = []
    pending_start: datetime | None = None
    for i, seg in enumerate(segments):
        seg = replace(seg, start=pending_start or seg.start)
        pending_start = None
        is_last = i == len(segments) - 1
        short = (seg.end - seg.start).total_seconds() < min_interval_s and not is_last
        if short and out:
            out[-1].end = seg.end
        elif short:
            pending_start = seg.start  # nothing before it: give its time to the next segment
        else:
            out.append(seg)
    return _merge(out)  # gluing may have made equal neighbours adjacent


def build_intervals(
    samples: Sequence[Sample],
    rules: Rules,
    start: datetime,
    now: datetime,
    *,
    prev: Sample | None = None,
    carry: Interval | None = None,
    marks: Sequence[tuple[datetime, datetime | None]] = (),
) -> list[Interval]:
    """Build the timeline for [start, now].

    ``samples`` are sorted and lie in [start, now]; ``prev`` is the last sample
    before ``start``. ``carry`` describes the already known state right before
    ``start`` (state may be NOT_RUN) with the moment it began, so an ongoing
    stretch keeps its original start. The last returned interval is open.
    """
    points = ([prev] if prev else []) + [s for s in samples if s.ts <= now]
    segments = _base_segments(points, rules, start, now, marks)
    if carry is not None and carry.start < start:
        segments.insert(0, _Segment(carry.state, carry.start, start, carry.program))
    segments = _merge_and_debounce(segments, rules.min_interval_s)

    threshold = timedelta(seconds=rules.idle_to_stop_s)
    intervals: list[Interval] = []
    for seg in segments:
        if seg.state != NOT_RUN:
            intervals.append(Interval(seg.state, seg.start, seg.end, seg.program))
        elif seg.end - seg.start <= threshold:
            intervals.append(Interval(IDLE, seg.start, seg.end, seg.program))
        else:
            intervals.append(Interval(IDLE, seg.start, seg.start + threshold, seg.program))
            intervals.append(Interval(STOP, seg.start + threshold, seg.end, seg.program))
    if intervals:
        intervals[-1] = replace(intervals[-1], end=None)
    return intervals


def part_events(samples: Sequence[Sample], prev_count: int | None) -> list[PartEvent]:
    """Parts produced, from increments of the machine part counter.

    A decreasing counter is a reset by the operator, never negative output:
    the new value is what was made since the reset.
    """
    events: list[PartEvent] = []
    last = prev_count
    for sample in samples:
        count = sample.part_count
        if count is None:
            continue
        if last is not None and count != last:
            delta = count - last if count > last else count
            if delta > 0:
                events.append(PartEvent(sample.ts, sample.program, delta))
        last = count
    return events
