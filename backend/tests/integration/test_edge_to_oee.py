"""End to end on real services: edge collector -> Mosquitto -> ingest -> worker -> OEE.

The simulated machine runs on a virtual clock, so "ten minutes without the
broker" takes seconds. Every scenario must deliver every message exactly
once (no seq gaps) and end with the same OEE as the undisturbed run.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from collector.buffer import Buffer
from collector.config import MqttConfig
from collector.drivers.simulator import Scenario, SimulatorDriver
from collector.messages import MessageFactory
from collector.poller import MachinePoller
from collector.publisher import MqttPublisher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.core.config import Settings
from app.ingest.consumer import run_forever
from app.ingest.health import find_seq_gaps
from app.ingest.service import IngestService
from app.worker.__main__ import Worker
from tests.conftest import seed_reference

Factory = async_sessionmaker[AsyncSession]

MOSQUITTO_CONF = """\
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
max_inflight_messages 500
max_queued_messages 1000000
"""

# 30 simulated minutes; one poll per simulated second.
SCENARIO = Scenario.model_validate(
    {
        "name": "integration",
        "loop": False,
        "program": "O1",
        "steps": [
            {"state": "run", "duration_s": 600, "cycle_time_s": 50, "load_time_s": 10},
            {"state": "idle", "duration_s": 300},
            {"state": "alarm", "duration_s": 120},
            {"state": "run", "duration_s": 600, "cycle_time_s": 50, "load_time_s": 10},
            {"state": "idle", "duration_s": 180},
        ],
    }
)
DURATION_S = 1800

# Reference result, by hand from the scenario (hour bucket that contains the run):
#   RUN    2 x 10 cycles x 50 s                                        = 1000 s, 20 parts
#   ALARM  900..1020                                                    =  120 s
#   pauses between cycles 2 x 9 x 10 s (IDLE)                           =  180 s
#   590..900 without running: IDLE 120 s + STOP 190 s
#   1610..1820 (last sample at 1790 is trusted for 30 s): IDLE 120 s + STOP 90 s
#   after 1820 no data: OFFLINE 3600 - 1820                             = 1780 s
#   norm 45 s per part: performance = 20 x 45 / 1000 = 0.9
REFERENCE = {
    "planned_time_s": 3600.0,
    "run_time_s": 1000.0,
    "alarm_s": 120.0,
    "idle_s": 420.0,
    "stop_s": 280.0,
    "setup_s": 0.0,
    "offline_s": 1780.0,
    "total_parts": 20,
    "good_parts": 20,
    "availability": 1000 / 3600,
    "performance": 0.9,
    "quality": 1.0,
    "oee": 1000 / 3600 * 0.9,
}


class VirtualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@dataclass
class Broker:
    container: DockerContainer
    port: int

    def stop(self) -> None:
        self.container.get_wrapped_container().stop(timeout=10)

    def start(self) -> None:
        self.container.get_wrapped_container().start()


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(MOSQUITTO_CONF)
    conf.chmod(0o644)
    with socket.socket() as probe:  # a fixed host port survives a restart of the container
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    container = (
        DockerContainer("eclipse-mosquitto:2")
        .with_volume_mapping(str(conf), "/mosquitto/config/mosquitto.conf", "ro")
        .with_bind_ports(1883, port)
        .waiting_for(LogMessageWaitStrategy(re.compile(r"mosquitto version .* running")))
    )
    with container:
        yield Broker(container, port)


class Collector:
    """The edge collector wired exactly as collector.main does, on a virtual clock."""

    def __init__(self, buffer_path: Path, port: int, clock: VirtualClock, start: datetime) -> None:
        self.buffer = Buffer(str(buffer_path))
        self.publisher = MqttPublisher(MqttConfig(host="127.0.0.1", port=port, keepalive_s=5), "edge-it", self.buffer)
        self.clock = clock
        self.poller = MachinePoller(
            "m1",
            # the scenario position survives a collector restart: it follows the clock, not the process
            SimulatorDriver(SCENARIO, clock=clock, offset_s=clock.value),
            MessageFactory("t1", "edge-it", "m1", partial(self.buffer.next_seq, "m1")),
            self.publisher,
            now=lambda: start + timedelta(seconds=clock.value),
            monotonic=clock,
        )
        self.publisher.start()

    async def run(self, first: int, last: int) -> None:
        def polls() -> None:
            for second in range(first, last):
                self.clock.value = float(second)
                self.poller.poll_once()

        await asyncio.to_thread(polls)

    def shutdown(self) -> None:
        self.publisher.stop()
        self.buffer.close()


async def wait_until(condition: Any, timeout_s: float = 90.0, what: str = "condition") -> None:
    async with asyncio.timeout(timeout_s):
        while not await asyncio.to_thread(condition):
            await asyncio.sleep(0.2)


@pytest.mark.parametrize("disturbance", ["none", "broker_down_10_min", "collector_restart_with_full_buffer"])
async def test_edge_to_oee(
    disturbance: str, broker: Broker, session_factory: Factory, service: IngestService, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    machine = await seed_reference(session_factory)
    settings = Settings(mqtt_host="127.0.0.1", mqtt_port=broker.port, ingest_client_id=f"ingest-{disturbance}")
    ingest = asyncio.create_task(run_forever(settings, service))
    await asyncio.sleep(1.5)  # ingest has subscribed

    clock = VirtualClock()
    buffer_path = tmp_path / "buffer.db"
    collector = Collector(buffer_path, broker.port, clock, start)
    try:
        if disturbance == "none":
            await collector.run(0, DURATION_S)
        else:
            await collector.run(0, 300)
            await wait_until(lambda: collector.buffer.size() == 0, what="first minutes delivered")
            await asyncio.to_thread(broker.stop)
            await wait_until(lambda: not collector.publisher.connected, what="collector noticed the outage")
            await collector.run(300, 900)  # ten simulated minutes with no broker
            buffered = collector.buffer.size()
            assert buffered > 200, "messages must pile up in the SQLite buffer"

            if disturbance == "collector_restart_with_full_buffer":
                collector.shutdown()  # process restart: only the buffer file survives
                collector = Collector(buffer_path, broker.port, clock, start)
                assert collector.buffer.size() == buffered
            await asyncio.to_thread(broker.start)
            await collector.run(900, DURATION_S)
        await wait_until(lambda: collector.buffer.size() == 0, what="buffer drained")

        async def stored() -> int:
            async with session_factory() as session:
                return int((await session.execute(text("SELECT max(seq) FROM telemetry"))).scalar_one() or 0)

        last_seq = collector.buffer.next_seq("m1") - 1
        async with asyncio.timeout(90):
            while await stored() < last_seq - 3:  # the last few messages may be events
                await asyncio.sleep(0.2)
        await asyncio.sleep(1.0)
    finally:
        collector.shutdown()
        ingest.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ingest

    async with session_factory() as session:
        # every message exactly once: seq is 1..N without holes or duplicates
        seqs = [
            r.seq
            for r in await session.execute(
                text("SELECT seq FROM telemetry UNION ALL SELECT seq FROM machine_events ORDER BY seq")
            )
        ]
        assert seqs == list(range(1, last_seq + 1))
        assert await find_seq_gaps(session, now + timedelta(minutes=10)) == []
        assert (await session.execute(text("SELECT count(*) FROM ingest_rejects"))).scalar_one() == 0
        events = [r.type for r in await session.execute(text("SELECT type FROM machine_events ORDER BY ts, seq"))]
    assert events == ["alarm_raised", "alarm_cleared"]

    worker = Worker(settings, session_factory)
    for _ in range(20):
        await worker.rebuild_timeline(machine, now)
        await worker.update_oee(machine, now)
    async with session_factory() as session:
        row = (
            (await session.execute(text("SELECT * FROM oee_hourly WHERE period_start = :start"), {"start": start}))
            .mappings()
            .one()
        )
    assert {name: row[name] for name in REFERENCE} == pytest.approx(REFERENCE)
