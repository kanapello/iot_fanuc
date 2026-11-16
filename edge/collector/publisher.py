"""MQTT publisher shared by all pollers (QoS 1)."""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from collector.config import MqttConfig
from collector.messages import OutgoingMessage

log = logging.getLogger(__name__)


class Publisher(Protocol):
    def publish(self, message: OutgoingMessage) -> None: ...


class MqttPublisher:
    """Thread-safe wrapper around paho-mqtt.

    paho keeps QoS 1 messages in memory while the broker is unreachable and
    resends them after reconnect. Surviving a collector restart is the job of
    the SQLite store-and-forward buffer (stage 2).
    """

    def __init__(self, config: MqttConfig, client_id: str) -> None:
        self._config = config
        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
        )
        if config.username:
            password = os.environ.get(config.password_env) if config.password_env else None
            self._client.username_pw_set(config.username, password)
        if config.tls:
            self._client.tls_set()
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        if reason_code.is_failure:
            log.error("MQTT connect refused: %s", reason_code)
        else:
            log.info("MQTT connected to %s:%s", self._config.host, self._config.port)

    def _on_disconnect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        log.warning("MQTT disconnected: %s", reason_code)

    def start(self) -> None:
        # connect_async + loop_start: the network thread retries until the broker is reachable.
        self._client.connect_async(self._config.host, self._config.port, keepalive=self._config.keepalive_s)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, message: OutgoingMessage) -> None:
        self._client.publish(message.topic, message.payload, qos=1, retain=message.retain)
