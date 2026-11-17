"""The production broker configuration: every collector writes only into its own site (NOW.md 2.2, 2.7)."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import aiomqtt
import pytest
from testcontainers.core.container import DockerContainer

DEPLOY = Path(__file__).resolve().parents[3] / "deploy" / "mosquitto"
COLLECTORS = "col-a1:acme:a1:pass-a1,col-a2:acme:a2:pass-a2,col-b1:bravo:b1:pass-b1"


@pytest.fixture(scope="module")
def broker() -> Iterator[int]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    container = (
        DockerContainer("eclipse-mosquitto:2")
        .with_env("MM_MQTT_BACKEND_USER", "backend")
        .with_env("MM_MQTT_BACKEND_PASSWORD", "pass-backend")
        .with_env("MM_MQTT_COLLECTORS", COLLECTORS)
        .with_volume_mapping(str(DEPLOY / "mosquitto.conf"), "/mosquitto/config/mosquitto.conf", "ro")
        .with_volume_mapping(str(DEPLOY / "docker-entrypoint.sh"), "/docker-entrypoint-mm.sh", "ro")
        .with_kwargs(entrypoint=["/docker-entrypoint-mm.sh"])
        .with_bind_ports(1883, port)
    )
    with container:
        _wait_for_broker(port)
        yield port


def _wait_for_broker(port: int, timeout_s: float = 30.0) -> None:
    """The published port accepts TCP before mosquitto listens: wait for a real MQTT login."""
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        client = mqtt.Client(CallbackAPIVersion.VERSION2)
        client.username_pw_set("backend", "pass-backend")
        try:
            if (
                client.connect("127.0.0.1", port, keepalive=5) == 0
                and client.loop(timeout=2.0) == 0
                and client.is_connected()
            ):
                client.disconnect()
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise TimeoutError("broker did not accept the backend login")


async def test_collector_writes_only_into_its_own_site(broker: int) -> None:
    received: list[str] = []

    async with aiomqtt.Client("127.0.0.1", broker, username="backend", password="pass-backend") as reader:
        await reader.subscribe("mm/v1/#", qos=1)

        async def collect() -> None:
            async for message in reader.messages:
                received.append(str(message.topic))

        task = asyncio.create_task(collect())
        snoop: list[str] = []
        async with (
            aiomqtt.Client("127.0.0.1", broker, username="col-a1", password="pass-a1") as collector,
            aiomqtt.Client("127.0.0.1", broker, username="col-a2", password="pass-a2") as neighbour,
        ):
            # collectors may read nothing, not even their neighbours' data
            with contextlib.suppress(aiomqtt.MqttError):
                await neighbour.subscribe("mm/v1/#", qos=1)

            async def spy() -> None:
                async for message in neighbour.messages:
                    snoop.append(str(message.topic))

            spying = asyncio.create_task(spy())
            for topic in (
                "mm/v1/acme/a1/m1/telemetry",  # own site
                "mm/v1/acme/a1/m1/event",
                "mm/v1/acme/a1/collector/col-a1/heartbeat",
                "mm/v1/acme/a2/m3/telemetry",  # another site of the same organization
                "mm/v1/bravo/b1/m1/telemetry",  # another organization
                "mm/v1/bravo/b1/m1/event",
                "mm/v1/acme/a1/collector/col-a2/heartbeat",  # posing as another collector
                "mm/v1/acme/a1/m1/telemetry/extra",
            ):
                await collector.publish(topic, b"{}", qos=1)
            await asyncio.sleep(1.0)
            spying.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await spying
        assert snoop == []
        await asyncio.sleep(1.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert sorted(set(received)) == [
        "mm/v1/acme/a1/collector/col-a1/heartbeat",
        "mm/v1/acme/a1/m1/event",
        "mm/v1/acme/a1/m1/telemetry",
    ]


async def test_wrong_password_and_unknown_account_are_refused(broker: int) -> None:
    for username, password in (("col-a1", "pass-b1"), ("intruder", "x"), (None, None)):
        with pytest.raises(aiomqtt.MqttError):
            async with aiomqtt.Client("127.0.0.1", broker, username=username, password=password):
                pass
