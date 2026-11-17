"""End-to-end: Mosquitto -> consumer -> TimescaleDB (testcontainers)."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Iterator

import aiomqtt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.core.config import Settings
from app.ingest.consumer import run_forever
from app.ingest.service import IngestService
from tests.conftest import encode, make_message, seed_reference

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
def broker() -> Iterator[tuple[str, int]]:
    container = (
        DockerContainer("eclipse-mosquitto:2")
        .with_command("mosquitto -c /mosquitto-no-auth.conf")
        .with_exposed_ports(1883)
        .waiting_for(LogMessageWaitStrategy(re.compile(r"mosquitto version .* running")))
    )
    with container:
        yield container.get_container_host_ip(), int(container.get_exposed_port(1883))


async def publish(broker: tuple[str, int], messages: list[tuple[str, bytes]]) -> None:
    async with aiomqtt.Client(broker[0], broker[1]) as client:
        for topic, payload in messages:
            await client.publish(topic, payload, qos=1)


async def wait_for_count(factory: Factory, table: str, expected: int, timeout_s: float = 15.0) -> None:
    count = -1
    async with asyncio.timeout(timeout_s):
        while count != expected:
            await asyncio.sleep(0.1)
            async with factory() as session:
                count = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_messages_survive_ingest_downtime(
    broker: tuple[str, int], service: IngestService, session_factory: Factory
) -> None:
    await seed_reference(session_factory)
    settings = Settings(mqtt_host=broker[0], mqtt_port=broker[1], ingest_client_id="mm-ingest-test")

    task = asyncio.create_task(run_forever(settings, service))
    await asyncio.sleep(1.0)  # let the consumer subscribe
    first = [encode(make_message("telemetry", seq=i)) for i in range(1, 4)]
    await publish(broker, [*first, ("mm/v1/torg/t1/m1/telemetry", b"garbage"), encode(make_message("event", seq=4))])
    await wait_for_count(session_factory, "telemetry", 3)
    await wait_for_count(session_factory, "machine_events", 1)
    await wait_for_count(session_factory, "ingest_rejects", 1)
    await stop(task)

    # ingest is down: the broker must keep QoS 1 messages for its persistent session
    await publish(broker, [encode(make_message("telemetry", seq=i)) for i in range(5, 8)])
    await asyncio.sleep(0.5)
    await wait_for_count(session_factory, "telemetry", 3)

    task = asyncio.create_task(run_forever(settings, service))
    try:
        await wait_for_count(session_factory, "telemetry", 6)
        # nothing acknowledged earlier is delivered and stored twice
        await asyncio.sleep(0.5)
        await wait_for_count(session_factory, "machine_events", 1)
        await wait_for_count(session_factory, "ingest_rejects", 1)
    finally:
        await stop(task)
