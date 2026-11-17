from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeClock

from collector.config import load_config
from collector.drivers.base import DriverConnectionError
from collector.drivers.simulator import DEFAULT_SCENARIOS_DIR, Scenario, SimulatorDriver, load_scenario

EDGE_DIR = Path(__file__).resolve().parents[1]


def make(steps: list[dict[str, object]], clock: FakeClock, **kwargs: object) -> SimulatorDriver:
    scenario = Scenario.model_validate({"name": "t", "program": "O0001", "steps": steps, **kwargs})
    return SimulatorDriver(scenario, clock=clock)


def test_run_counts_parts_per_cycle(clock: FakeClock) -> None:
    driver = make([{"state": "run", "duration_s": 100, "cycle_time_s": 10}], clock, initial_part_count=5)
    snap = driver.read_snapshot()
    assert (snap.mode, snap.run, snap.motion, snap.part_count) == ("MEM", "START", True, 5)
    clock.advance(35)
    assert driver.read_snapshot().part_count == 8


def test_states_map_to_signals(clock: FakeClock) -> None:
    driver = make(
        [
            {"state": "run", "duration_s": 10, "cycle_time_s": 10},
            {"state": "idle", "duration_s": 10},
            {"state": "hold", "duration_s": 10},
            {"state": "setup", "duration_s": 10, "mode": "EDIT", "program": "O0002"},
            {"state": "alarm", "duration_s": 10, "emergency": True, "alarm": {"code": "SV0401", "message": "X"}},
        ],
        clock,
    )
    clock.advance(15)
    idle = driver.read_snapshot()
    assert (idle.run, idle.motion, idle.alarm, idle.part_count) == ("STOP", False, False, 1)
    clock.advance(10)
    assert driver.read_snapshot().run == "HOLD"
    clock.advance(10)
    setup = driver.read_snapshot()
    assert (setup.mode, setup.run, setup.program) == ("EDIT", "STOP", "O0002")
    clock.advance(10)
    alarm = driver.read_snapshot()
    assert alarm.alarm is True and alarm.emergency is True
    assert [a.code for a in driver.read_alarms()] == ["SV0401"]


def test_offline_raises_and_recovers(clock: FakeClock) -> None:
    driver = make(
        [{"state": "run", "duration_s": 10, "cycle_time_s": 5}, {"state": "offline", "duration_s": 10}], clock
    )
    driver.connect()
    clock.advance(12)
    with pytest.raises(DriverConnectionError):
        driver.read_snapshot()
    with pytest.raises(DriverConnectionError):
        driver.connect()
    clock.advance(10)  # next loop
    driver.connect()
    assert driver.read_snapshot().part_count == 2


def test_counter_reset_and_loop(clock: FakeClock) -> None:
    driver = make(
        [
            {"state": "run", "duration_s": 100, "cycle_time_s": 10},
            {"state": "counter_reset"},
            {"state": "run", "duration_s": 50, "cycle_time_s": 10},
        ],
        clock,
        initial_part_count=1000,
    )
    clock.advance(99)
    assert driver.read_snapshot().part_count == 1009
    clock.advance(2)  # after the reset
    assert driver.read_snapshot().part_count == 0
    clock.advance(60)  # second loop, 11 s in: 5 parts carried over the loop boundary + 1
    assert driver.read_snapshot().part_count == 6


def test_loop_without_reset_keeps_counting(clock: FakeClock) -> None:
    driver = make([{"state": "run", "duration_s": 100, "cycle_time_s": 10}], clock)
    clock.advance(250)
    assert driver.read_snapshot().part_count == 25


def test_non_looping_scenario_stays_in_last_step(clock: FakeClock) -> None:
    driver = make(
        [{"state": "run", "duration_s": 20, "cycle_time_s": 10}, {"state": "idle", "duration_s": 5}],
        clock,
        loop=False,
    )
    clock.advance(1000)
    snap = driver.read_snapshot()
    assert (snap.run, snap.part_count) == ("STOP", 2)


def test_cycle_with_part_change_pause(clock: FakeClock) -> None:
    driver = make([{"state": "run", "duration_s": 100, "cycle_time_s": 8, "load_time_s": 2}], clock)
    assert driver.read_snapshot().run == "START"
    clock.advance(7)
    assert driver.read_snapshot().part_count == 0
    clock.advance(1.5)  # 8.5 s: cycle finished, machine waits for the next blank
    snap = driver.read_snapshot()
    assert (snap.run, snap.run_code, snap.aut_code, snap.alarm_code, snap.part_count) == ("STOP", 1, 1, 0, 1)
    clock.advance(2)  # 10.5 s: second cycle is running
    assert (driver.read_snapshot().run, driver.read_snapshot().run_code) == ("START", 3)
    clock.advance(30)  # 40.5 s: four full periods
    assert driver.read_snapshot().part_count == 4


def test_bundled_scenarios_and_configs_are_valid() -> None:
    names = {p.stem for p in DEFAULT_SCENARIOS_DIR.glob("*.yaml")}
    assert {
        "milling_normal",
        "frequent_stops",
        "alarm",
        "connection_loss",
        "counter_reset",
        "program_change",
    } <= names
    for name in names:
        assert load_scenario(name).name == name

    sim = load_config(EDGE_DIR / "config.sim.yaml")
    assert 5 <= len(sim.machines) <= 8
    for machine in sim.machines:
        assert machine.driver == "simulator"
        load_scenario(machine.profile)
    assert load_config(EDGE_DIR / "config.example.yaml").machines[0].driver == "fanuc_focas"
