"""The collector reaches the broker of a cloud server over TLS by default (ADR 0010)."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from pydantic import ValidationError

from collector.buffer import Buffer
from collector.config import MqttConfig, load_config
from collector.publisher import MqttPublisher

EDGE_DIR = Path(__file__).resolve().parents[1]


def test_tls_on_8883_is_the_default() -> None:
    config = MqttConfig(host="mm.example.com")
    assert (config.tls, config.port, config.ca_file) == (True, 8883, None)


def test_plain_mqtt_only_when_asked() -> None:
    assert MqttConfig(host="mosquitto", tls=False).port == 1883
    assert MqttConfig(host="mm.example.com", port=18883).port == 18883


def test_own_ca_file_must_exist_and_needs_tls(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ca_file not found"):
        MqttConfig(host="mm.example.com", ca_file=str(tmp_path / "missing.crt"))
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    with pytest.raises(ValidationError, match="ca_file needs tls"):
        MqttConfig(host="mosquitto", tls=False, ca_file=str(ca))
    assert MqttConfig(host="mm.example.com", ca_file=str(ca)).ca_file == str(ca)


def test_publisher_checks_the_server_certificate_and_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """System root CAs, a required certificate and the host name checked: nothing is switched off."""
    loaded: list[ssl.SSLContext] = []
    original = ssl.SSLContext.load_default_certs

    def load_default_certs(self: ssl.SSLContext, purpose: ssl.Purpose = ssl.Purpose.SERVER_AUTH) -> None:
        loaded.append(self)
        original(self, purpose)

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", load_default_certs)
    buffer = Buffer(str(tmp_path / "buffer.db"))
    try:
        publisher = MqttPublisher(MqttConfig(host="mm.example.com"), "edge-1", buffer)
        context: ssl.SSLContext = publisher._client._ssl_context
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert loaded == [context]  # the system root CAs

        plain = MqttPublisher(MqttConfig(host="mosquitto", tls=False), "edge-2", buffer)
        assert plain._client._ssl_context is None
    finally:
        buffer.close()


def test_shipped_configs() -> None:
    """The example (a real collector) uses TLS; the simulators of `make up` stay inside the compose network."""
    example = load_config(EDGE_DIR / "config.example.yaml").mqtt
    assert (example.tls, example.port) == (True, 8883)
    for name in ("config.sim.a.yaml", "config.sim.b.yaml"):
        sim = load_config(EDGE_DIR / name).mqtt
        assert (sim.host, sim.tls, sim.port) == ("mosquitto", False, 1883)
