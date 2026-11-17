"""Collector entry point: ``python -m collector.main --config config.yaml``."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial

from collector.buffer import Buffer
from collector.config import CollectorConfig, FanucFocasMachine, MachineConfig, SimulatorMachine, load_config
from collector.drivers.base import MachineDriver
from collector.drivers.simulator import SimulatorDriver, load_scenario
from collector.health import run_heartbeat
from collector.messages import MessageFactory
from collector.poller import MachinePoller
from collector.publisher import MqttPublisher

log = logging.getLogger("collector")


class LaggedClock:
    """Clock that runs ``lag_s`` behind real time; the simulator uses it to replay history."""

    def __init__(self, lag_s: float = 0.0) -> None:
        self.lag_s = lag_s

    def monotonic(self) -> float:
        return time.monotonic() - self.lag_s

    def now(self) -> datetime:
        return datetime.now(UTC) - timedelta(seconds=self.lag_s)


def build_driver(config: CollectorConfig, machine: MachineConfig, clock: LaggedClock) -> MachineDriver:
    if isinstance(machine, FanucFocasMachine):
        if config.focas is None:
            raise ValueError("config section 'focas.library_path' is required for fanuc_focas machines")
        from collector.drivers.fanuc_focas import FanucFocasDriver

        return FanucFocasDriver(config.focas.library_path, machine.ip, machine.port, machine.part_counter)
    scenario = load_scenario(machine.profile, config.scenarios_dir)
    return SimulatorDriver(scenario, offset_s=machine.offset_s, time_scale=machine.time_scale, clock=clock.monotonic)


def run_machine(poller: MachinePoller, clock: LaggedClock, step_s: float, stop: threading.Event) -> None:
    """Replay the simulated past in fast-forward (if any), then poll in real time."""
    if clock.lag_s > 0:
        log.info("%s: replaying %.1f h of simulated history", poller.machine_id, clock.lag_s / 3600)
        while clock.lag_s > 0 and not stop.is_set():
            poller.poll_once()
            clock.lag_s = max(0.0, clock.lag_s - step_s)
        log.info("%s: history replayed, switching to real time", poller.machine_id)
    poller.run(stop)


def run(config: CollectorConfig, stop: threading.Event) -> None:
    buffer = Buffer(config.buffer.path, config.buffer.max_age_days)
    publisher = MqttPublisher(config.mqtt, client_id=config.collector_id, buffer=buffer)
    pollers: list[MachinePoller] = []
    threads: list[threading.Thread] = []
    for machine in config.machines:
        clock = LaggedClock()
        step_s = 1.0
        if isinstance(machine, SimulatorMachine) and not buffer.has_seq(machine.id):
            clock.lag_s, step_s = machine.backfill_hours * 3600, machine.backfill_step_s
        poller = MachinePoller(
            machine.id,
            build_driver(config, machine, clock),
            MessageFactory(config.site, config.collector_id, machine.id, partial(buffer.next_seq, machine.id)),
            publisher,
            poll_interval_s=machine.poll_interval_ms / 1000,
            keyframe_interval_s=config.keyframe_interval_s,
            now=clock.now,
            monotonic=clock.monotonic,
        )
        pollers.append(poller)
        threads.append(
            threading.Thread(target=run_machine, args=(poller, clock, step_s, stop), name=f"poller-{machine.id}")
        )
    publisher.start()
    threads.append(
        threading.Thread(
            target=run_heartbeat,
            args=(
                config.site,
                config.collector_id,
                publisher,
                buffer,
                lambda: {p.machine_id: p.connected for p in pollers},
                stop,
            ),
            name="heartbeat",
        )
    )
    for thread in threads:
        thread.start()
    log.info("collector %s started with %d machine(s)", config.collector_id, len(pollers))
    stop.wait()
    for thread in threads:
        thread.join(timeout=5)
    publisher.stop()
    buffer.close()
    log.info("collector stopped")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Machine monitor edge collector")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    run(load_config(args.config), stop)


if __name__ == "__main__":
    main()
