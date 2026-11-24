"""Cycle split: from one part to the next, what the time went to.

A cycle is the time between neighbouring part events of a machine. A counter that grows by several
parts at once is one cycle of that many parts (a fixture with several workpieces). Inside a cycle:

* machine time — RUN;
* the gap — from the previous part to the first start of RUN: part change, or, when longer than the
  pause threshold, downtime;
* interruptions — IDLE, STOP, ALARM, SETUP after machining started;
* OFFLINE — the data is not complete.

The first part after a program change is set-up; a cycle with OFFLINE inside (or without any RUN) is
incomplete. Neither goes into the statistics. A cycle belongs to the shift in which its part was done.

What is stored (the ``cycles`` table) are only the facts of each cycle, computed from the timeline and
the part events of its own window, so a cycle is rebuilt whenever the timeline under it is rebuilt.
Splitting the gap into part change and downtime depends on the threshold (a machine setting, or by
default three times the median machine time of the program, at least 10 minutes) and is done when
read, so changing the threshold needs no recalculation.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.timeline.builder import ALARM, IDLE, OFFLINE, RUN, SETUP, STOP, Interval, PartEvent

NORMAL, SETUP_CYCLE, INCOMPLETE = "normal", "setup", "incomplete"
MIN_PAUSE_THRESHOLD_S = 600.0
PAUSE_FACTOR = 3.0


@dataclass(frozen=True, slots=True)
class Cycle:
    start: datetime  # the previous part
    end: datetime  # this part is done
    program: str | None
    parts: int
    machine_s: float  # RUN
    gap_s: float  # from the previous part to the first RUN (the whole cycle if there was no RUN)
    idle_s: float  # interruptions after machining started
    stop_s: float
    alarm_s: float
    setup_s: float
    offline_s: float
    kind: str  # normal | setup | incomplete

    @property
    def interrupt_s(self) -> float:
        return self.idle_s + self.stop_s + self.alarm_s + self.setup_s


def build_cycles(
    events: Sequence[PartEvent], previous: PartEvent | None, intervals: Sequence[Interval], now: datetime
) -> list[Cycle]:
    """Cycles ending at ``events`` (ordered by time); ``previous`` is the part event before the first one.

    ``intervals`` must cover the cycles; an open interval lasts until ``now``.
    """
    cycles: list[Cycle] = []
    before = previous
    for event in events:
        if before is not None and event.ts > before.ts:
            cycles.append(_cycle(before, event, intervals, now))
        before = event
    return cycles


def _cycle(before: PartEvent, event: PartEvent, intervals: Sequence[Interval], now: datetime) -> Cycle:
    start, end = before.ts, event.ts
    pieces = []
    for interval in intervals:
        lo, hi = max(interval.start, start), min(interval.end or now, end)
        if hi > lo:
            pieces.append((interval.state, lo, hi, interval.program))
    first_run = min((lo for state, lo, _hi, _p in pieces if state == RUN), default=None)
    seconds = dict.fromkeys((RUN, IDLE, STOP, ALARM, SETUP, OFFLINE), 0.0)
    for state, lo, hi, _program in pieces:
        if state in (RUN, OFFLINE) or (first_run is not None and lo >= first_run):
            seconds[state] = seconds.get(state, 0.0) + (hi - lo).total_seconds()
    gap = ((first_run or end) - start).total_seconds()
    changed = before.program != event.program or any(p is not None and p != event.program for _s, _lo, _hi, p in pieces)
    if seconds[OFFLINE] > 0 or first_run is None:
        kind = INCOMPLETE
    elif changed:
        kind = SETUP_CYCLE
    else:
        kind = NORMAL
    return Cycle(
        start=start,
        end=end,
        program=event.program,
        parts=event.delta,
        machine_s=seconds[RUN],
        gap_s=gap,
        idle_s=seconds[IDLE],
        stop_s=seconds[STOP],
        alarm_s=seconds[ALARM],
        setup_s=seconds[SETUP],
        offline_s=seconds[OFFLINE],
        kind=kind,
    )


def pause_threshold(machine_times: Sequence[float], fixed_s: float | None) -> float:
    """The machine setting, or three times the median machine time, at least 10 minutes."""
    if fixed_s is not None:
        return fixed_s
    median = statistics.median(machine_times) if machine_times else 0.0
    return max(PAUSE_FACTOR * median, MIN_PAUSE_THRESHOLD_S)


@dataclass(frozen=True, slots=True)
class Split:
    """How one cycle is counted: the gap is part change up to the threshold, downtime beyond it."""

    cycle: Cycle
    load_s: float  # part change
    pause_s: float  # downtime between parts

    @property
    def counted(self) -> bool:
        return self.cycle.kind == NORMAL


def split(cycles: Sequence[Cycle], threshold_s: float) -> list[Split]:
    return [
        Split(c, load_s=0.0 if c.gap_s > threshold_s else c.gap_s, pause_s=c.gap_s if c.gap_s > threshold_s else 0.0)
        for c in cycles
    ]
