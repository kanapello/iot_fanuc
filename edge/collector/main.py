"""Collector entry point: ``python -m collector.main --config config.yaml``."""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from collector.config import CollectorConfig, FanucFocasMachine, MachineConfig, load_config
from collector.drivers.base import MachineDriver
from collector.drivers.simulator import SimulatorDriver, load_scenario
from collector.messages import MessageFactory
from collector.poller import MachinePoller
from collector.publisher import MqttPublisher

log = logging.getLogger("collector")


def build_driver(config: CollectorConfig, machine: MachineConfig) -> MachineDriver:
    if isinstance(machine, FanucFocasMachine):
        raise NotImplementedError("driver 'fanuc_focas' is delivered in stage 2; use tools/focas_probe.py for now")
    scenario = load_scenario(machine.profile, config.scenarios_dir)
    return SimulatorDriver(scenario, offset_s=machine.offset_s, time_scale=machine.time_scale)


def run(config: CollectorConfig, stop: threading.Event) -> None:
    publisher = MqttPublisher(config.mqtt, client_id=config.collector_id)
    pollers = [
        MachinePoller(
            machine.id,
            build_driver(config, machine),
            MessageFactory(config.site, config.collector_id, machine.id),
            publisher,
            poll_interval_s=machine.poll_interval_ms / 1000,
            keyframe_interval_s=config.keyframe_interval_s,
        )
        for machine in config.machines
    ]
    publisher.start()
    threads = [threading.Thread(target=p.run, args=(stop,), name=f"poller-{p.machine_id}") for p in pollers]
    for thread in threads:
        thread.start()
    log.info("collector %s started with %d machine(s)", config.collector_id, len(pollers))
    stop.wait()
    for thread in threads:
        thread.join(timeout=5)
    publisher.stop()
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
