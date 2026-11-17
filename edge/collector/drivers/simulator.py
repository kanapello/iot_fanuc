"""Simulated machine driven by a YAML scenario (edge/scenarios/*.yaml).

The simulated state is a pure function of scenario time, so the same scenario
always produces the same signals; tests inject the clock.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from collector.drivers.base import Alarm, DriverConnectionError, MachineSnapshot

DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"

StepState = Literal["run", "idle", "hold", "setup", "alarm", "offline", "counter_reset"]


class AlarmSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = "SV0401"
    message: str = "V-READY OFF"
    category: str | None = "servo"


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: StepState
    duration_s: float = Field(default=0.0, ge=0)
    program: str | None = None  # program loaded from this step on
    cycle_time_s: float = Field(default=60.0, gt=0)  # machining time of one cycle
    load_time_s: float = Field(default=0.0, ge=0)  # part change after each cycle (machine stopped)
    parts_per_cycle: int = Field(default=1, ge=0)
    spindle_speed: float = 6000.0
    feed: float = 1000.0
    spindle_load: float = 35.0
    mode: str | None = None  # override of the operation mode (setup: EDIT / MDI / JOG)
    emergency: bool = False
    alarm: AlarmSpec = AlarmSpec()

    @model_validator(mode="after")
    def _check_duration(self) -> Step:
        if self.state == "counter_reset":
            self.duration_s = 0.0
        elif self.duration_s <= 0:
            raise ValueError(f"step '{self.state}' needs duration_s > 0")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    loop: bool = True
    program: str = "O0001"
    initial_part_count: int = Field(default=0, ge=0)
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_total(self) -> Scenario:
        if self.total_duration_s <= 0:
            raise ValueError("scenario needs at least one step with a duration")
        return self

    @property
    def total_duration_s(self) -> float:
        return sum(step.duration_s for step in self.steps)


def load_scenario(name: str, scenarios_dir: str | Path | None = None) -> Scenario:
    """Load a scenario by profile name (file ``<name>.yaml``) or by explicit path."""
    candidate = Path(name)
    if candidate.suffix not in (".yaml", ".yml"):
        candidate = Path(scenarios_dir or DEFAULT_SCENARIOS_DIR) / f"{name}.yaml"
    with open(candidate, encoding="utf-8") as fh:
        return Scenario.model_validate(yaml.safe_load(fh))


def _parts(step: Step, elapsed_s: float) -> int:
    """Parts finished ``elapsed_s`` into a run step (a part is counted when its cycle ends)."""
    if step.state != "run":
        return 0
    full, pos = divmod(elapsed_s, step.cycle_time_s + step.load_time_s)
    done = int(full) + (1 if step.load_time_s > 0 and pos >= step.cycle_time_s else 0)
    return done * step.parts_per_cycle


AUT_CODES = {"MDI": 0, "MEM": 1, "EDIT": 3, "HND": 4, "JOG": 5, "REF": 9, "RMT": 10}
RUN_CODES = {"RESET": 0, "STOP": 1, "HOLD": 2, "START": 3, "MSTR": 4}


def _with_codes(snapshot: MachineSnapshot) -> MachineSnapshot:
    return replace(
        snapshot,
        aut_code=AUT_CODES.get(snapshot.mode or ""),
        run_code=RUN_CODES.get(snapshot.run or ""),
        alarm_code=1 if snapshot.alarm else 0,
        emergency_code=1 if snapshot.emergency else 0,
    )


class SimulatorDriver:
    """MachineDriver implementation backed by a scenario."""

    def __init__(
        self,
        scenario: Scenario,
        *,
        offset_s: float = 0.0,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        anchor: float | None = None,
    ) -> None:
        self._scenario = scenario
        self._offset_s = offset_s
        self._time_scale = time_scale
        self._clock = clock
        # Scenario time 0 is ``anchor`` on the given clock; None = the moment the driver is created.
        # The collector anchors it to the Unix epoch on wall-clock time, so a restart continues
        # the same simulated history instead of starting the scenario (and its counters) again.
        self._t0 = clock() if anchor is None else anchor

        steps = scenario.steps
        reset_indexes = [i for i, s in enumerate(steps) if s.state == "counter_reset"]
        self._loop_has_reset = bool(reset_indexes)
        tail = steps[reset_indexes[-1] + 1 :] if reset_indexes else steps
        self._loop_tail_parts = sum(_parts(s, s.duration_s) for s in tail)
        programs = [s.program for s in steps if s.program]
        self._loop_end_program = programs[-1] if programs else scenario.program

    def connect(self) -> None:
        step, *_ = self._locate()
        if step.state == "offline":
            raise DriverConnectionError("simulated machine is unreachable")

    def disconnect(self) -> None:
        return None

    def scenario_time(self) -> float:
        return (self._clock() - self._t0) * self._time_scale + self._offset_s

    def _locate(self) -> tuple[Step, float, int, str, float]:
        """Return (active step, scenario time, part count, loaded program, time into the step)."""
        scenario = self._scenario
        t = max(0.0, self.scenario_time())
        total = scenario.total_duration_s
        loops, rem = divmod(t, total) if scenario.loop else (0.0, min(t, total))

        count = scenario.initial_part_count
        program = scenario.program
        if loops >= 1:
            program = self._loop_end_program
            if self._loop_has_reset:
                count = self._loop_tail_parts
            else:
                count += int(loops) * self._loop_tail_parts

        timed = [s for s in scenario.steps if s.duration_s > 0]
        for step in scenario.steps:
            if step.state == "counter_reset":
                count = 0
                continue
            if step.program:
                program = step.program
            is_last = step is timed[-1]
            if rem < step.duration_s or is_last:
                elapsed = min(rem, step.duration_s)
                return step, t, count + _parts(step, elapsed), program, elapsed
            count += _parts(step, step.duration_s)
            rem -= step.duration_s
        raise AssertionError("unreachable: scenario has a timed step")

    def read_snapshot(self) -> MachineSnapshot:
        return _with_codes(self._read())

    def _read(self) -> MachineSnapshot:
        step, t, count, program, elapsed = self._locate()
        if step.state == "offline":
            raise DriverConnectionError("simulated machine is unreachable")

        base = MachineSnapshot(
            program=program,
            program_name=program,
            part_count=count,
            feed_override=100.0,
            spindle_override=100.0,
            emergency=False,
            alarm=False,
            motion=False,
            feed_actual=0.0,
            spindle_speed=0.0,
            spindle_load=0.0,
        )
        if step.state == "run" and elapsed % (step.cycle_time_s + step.load_time_s) >= step.cycle_time_s:
            return replace(base, mode=step.mode or "MEM", run="STOP")  # part change between cycles
        if step.state == "run":
            load = round((step.spindle_load + 5.0 * math.sin(t / 3.0)) * 2) / 2
            feed = round(step.feed * (1.0 + 0.02 * math.sin(t / 2.0)) / 10) * 10
            return replace(
                base,
                mode=step.mode or "MEM",
                run="START",
                motion=True,
                feed_actual=float(feed),
                spindle_speed=step.spindle_speed,
                spindle_load=load,
            )
        if step.state == "hold":
            return replace(
                base, mode=step.mode or "MEM", run="HOLD", spindle_speed=step.spindle_speed, spindle_load=5.0
            )
        if step.state == "alarm":
            return replace(base, mode=step.mode or "MEM", run="STOP", alarm=True, emergency=step.emergency)
        return replace(base, mode=step.mode or ("JOG" if step.state == "setup" else "MEM"), run="STOP")

    def read_alarms(self) -> list[Alarm]:
        step, *_ = self._locate()
        if step.state == "offline":
            raise DriverConnectionError("simulated machine is unreachable")
        if step.state != "alarm":
            return []
        spec = step.alarm
        return [Alarm(code=spec.code, message=spec.message, category=spec.category)]
