"""A collector reaches the broker over TLS and its data arrives (ADR 0010).

The broker has the listeners of production: plain 1883 inside the server's network (ingest) and
TLS 8883 for collectors. The server certificate is issued for "localhost" by a test CA. The
collector checks the certificate and the name: with the system root CAs, or with the right CA but
another name, it does not connect at all, and its data waits in the buffer.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collector.buffer import Buffer
from collector.config import MqttConfig
from collector.messages import MessageFactory
from collector.publisher import MqttPublisher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer

from app.core.config import Settings
from app.ingest.consumer import run_forever
from app.ingest.service import IngestService
from tests.conftest import make_message, seed_reference
from tests.integration.test_edge_to_oee import _wait_for_port, broker_conf

Factory = async_sessionmaker[AsyncSession]


def make_certificates(directory: Path) -> Path:
    """A test CA and a server certificate for "localhost" signed by it; returns the CA certificate."""

    def openssl(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True)

    openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=mm test CA",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout", "ca.key", "-out", "ca.crt",
    )  # fmt: skip
    openssl(
        "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost", "-keyout", "server.key", "-out", "server.csr"
    )
    (directory / "server.ext").write_text(
        "subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n"
    )
    openssl(
        "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
        "-extfile", "server.ext", "-out", "server.crt",
    )  # fmt: skip
    for path in directory.iterdir():
        path.chmod(0o644)  # read by the mosquitto user of the container
    return directory / "ca.crt"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass
class TlsBroker:
    plain_port: int  # ingest, inside the server's network
    tls_port: int  # collectors
    ca: Path


@pytest.fixture
def tls_broker(tmp_path: Path) -> Iterator[TlsBroker]:
    certs = tmp_path / "certs"
    certs.mkdir()
    ca = make_certificates(certs)
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        broker_conf() + "listener 8883\ncertfile /certs/server.crt\nkeyfile /certs/server.key\ntls_version tlsv1.2\n"
    )
    conf.chmod(0o644)
    plain_port, tls_port = free_port(), free_port()
    container = (
        DockerContainer("eclipse-mosquitto:2")
        .with_volume_mapping(str(conf), "/mosquitto/config/mosquitto.conf", "ro")
        .with_volume_mapping(str(certs), "/certs", "ro")
        .with_bind_ports(1883, plain_port)
        .with_bind_ports(8883, tls_port)
    )
    with container:
        _wait_for_port(plain_port)
        yield TlsBroker(plain_port, tls_port, ca)


class Collector:
    def __init__(self, config: MqttConfig, buffer_path: Path) -> None:
        self.buffer = Buffer(str(buffer_path))
        self.publisher = MqttPublisher(config, "edge-tls", self.buffer)
        self.messages = MessageFactory("torg", "t1", "edge-tls", "m1", lambda: self.buffer.next_seq("m1"))
        self.publisher.start()

    def send(self, count: int) -> None:
        now = datetime.now(UTC)
        data = make_message("telemetry")["data"]
        for i in range(count):
            self.publisher.publish(self.messages.telemetry(now - timedelta(seconds=count - i), data))

    def pending(self) -> int:
        return self.buffer.size()

    def close(self) -> None:
        self.publisher.stop()
        self.buffer.close()


async def telemetry_rows(factory: Factory) -> int:
    async with factory() as session:
        return int((await session.execute(text("SELECT count(*) FROM telemetry"))).scalar_one())


async def test_collector_sends_over_tls_and_checks_the_certificate(
    tls_broker: TlsBroker, session_factory: Factory, service: IngestService, tmp_path: Path
) -> None:
    await seed_reference(session_factory, collector="edge-tls")
    settings = Settings(mqtt_host="127.0.0.1", mqtt_port=tls_broker.plain_port, ingest_client_id="ingest-tls")
    ingest = asyncio.create_task(run_forever(settings, service))
    collectors: list[Collector] = []
    try:
        await asyncio.sleep(1.5)  # ingest has subscribed

        # the right CA and the name of the certificate: connects, the data reaches the database
        good = Collector(
            MqttConfig(host="localhost", port=tls_broker.tls_port, ca_file=str(tls_broker.ca)), tmp_path / "a.db"
        )
        collectors.append(good)
        good.send(20)
        deadline = time.monotonic() + 60
        while await telemetry_rows(session_factory) < 20:
            assert time.monotonic() < deadline, "telemetry over TLS did not arrive"
            await asyncio.sleep(0.3)
        assert good.publisher.connected
        await asyncio.sleep(1)
        assert good.pending() == 0  # acknowledged by the broker, removed from the buffer

        # the system root CAs do not trust the test CA; the right CA with another name fails the name check
        untrusted = Collector(MqttConfig(host="localhost", port=tls_broker.tls_port), tmp_path / "b.db")
        wrong_name = Collector(
            MqttConfig(host="127.0.0.1", port=tls_broker.tls_port, ca_file=str(tls_broker.ca)), tmp_path / "c.db"
        )
        collectors += [untrusted, wrong_name]
        for collector in (untrusted, wrong_name):
            collector.send(5)
        await asyncio.sleep(6)
        for collector in (untrusted, wrong_name):
            assert not collector.publisher.connected
            assert collector.pending() == 5  # nothing left the buffer
        assert await telemetry_rows(session_factory) == 20
    finally:
        for collector in collectors:
            collector.close()
        ingest.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ingest
