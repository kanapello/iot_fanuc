"""MQTT publisher shared by all pollers: buffer first, publish, delete after PUBACK."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Protocol

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from collector.buffer import Buffer
from collector.config import MqttConfig
from collector.messages import OutgoingMessage

log = logging.getLogger(__name__)

MAX_IN_FLIGHT = 500


class Publisher(Protocol):
    def publish(self, message: OutgoingMessage) -> None: ...


class MqttPublisher:
    """Thread-safe publisher backed by the SQLite buffer.

    A sender thread reads the buffer in order and publishes with QoS 1; the
    row is deleted in the PUBACK callback. After a reconnect everything still
    in the buffer is sent again (the server is idempotent by ``msg_id``).
    """

    def __init__(self, config: MqttConfig, client_id: str, buffer: Buffer) -> None:
        self._config = config
        self._buffer = buffer
        self._lock = threading.Lock()
        self._in_flight: dict[int, int] = {}  # mid -> buffer row id
        self._last_sent_id = 0
        self._connected = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._send_loop, name="mqtt-sender", daemon=True)

        self._client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=True, protocol=mqtt.MQTTv311
        )
        if config.username:
            password = os.environ.get(config.password_env) if config.password_env else None
            self._client.username_pw_set(config.username, password)
        if config.tls:
            self._client.tls_set()
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _on_connect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        if reason_code.is_failure:
            log.error("MQTT connect refused: %s", reason_code)
            return
        with self._lock:
            self._in_flight.clear()
            self._last_sent_id = 0  # resend everything that was never acknowledged
        log.info(
            "MQTT connected to %s:%s, %d message(s) in buffer",
            self._config.host,
            self._config.port,
            self._buffer.size(),
        )
        self._connected.set()
        self._wake.set()

    def _on_disconnect(self, _client: Any, _userdata: Any, _flags: Any, reason_code: Any, _props: Any) -> None:
        self._connected.clear()
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_publish(self, _client: Any, _userdata: Any, mid: int, _reason_code: Any, _props: Any) -> None:
        with self._lock:
            row_id = self._in_flight.pop(mid, None)
        if row_id is not None:
            self._buffer.delete(row_id)
            self._wake.set()

    def start(self) -> None:
        removed = self._buffer.evict_expired()
        if removed:
            log.warning("dropped %d expired message(s) from the buffer", removed)
        # connect_async + loop_start: the network thread retries until the broker is reachable.
        self._client.connect_async(self._config.host, self._config.port, keepalive=self._config.keepalive_s)
        self._client.loop_start()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)
        self._client.loop_stop()
        self._client.disconnect()

    def publish(self, message: OutgoingMessage) -> None:
        if not message.buffered:
            if self.connected:
                self._client.publish(message.topic, message.payload, qos=1, retain=message.retain)
            return
        self._buffer.add(message.topic, message.payload, message.retain)
        self._wake.set()

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if not self._connected.is_set():
                continue
            with self._lock:
                free = MAX_IN_FLIGHT - len(self._in_flight)
                if free <= 0:
                    continue
                for row in self._buffer.pending(self._last_sent_id, free):
                    info = self._client.publish(row.topic, row.payload, qos=1, retain=row.retain)
                    self._in_flight[info.mid] = row.id
                    self._last_sent_id = row.id
