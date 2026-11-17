"""Building MQTT messages according to shared/schemas (contract v1)."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

TOPIC_PREFIX = "mm/v1"


def uuid7(unix_ms: int | None = None) -> str:
    """UUIDv7 (RFC 9562): 48-bit unix ms timestamp + 74 random bits."""
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def format_ts(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    topic: str
    payload: bytes
    retain: bool = False
    buffered: bool = True  # False: best effort, not stored in the buffer (heartbeat)


class _Counter:
    def __init__(self, first: int = 1) -> None:
        self._next = first

    def __call__(self) -> int:
        value = self._next
        self._next += 1
        return value


class MessageFactory:
    """Creates envelopes for one machine.

    ``next_seq`` supplies the per-machine counter shared by telemetry and
    events; the collector passes the persistent counter of the SQLite buffer.
    """

    def __init__(
        self, org: str, site: str, collector_id: str, machine_id: str, next_seq: Callable[[], int] | None = None
    ) -> None:
        self._org = org
        self._site = site
        self._collector_id = collector_id
        self._machine_id = machine_id
        self._next_seq = next_seq or _Counter()

    def _envelope(self, schema: str, kind: str, ts: datetime, data: dict[str, Any]) -> OutgoingMessage:
        body = {
            "schema": schema,
            "msg_id": uuid7(),
            "collector_id": self._collector_id,
            "machine_id": self._machine_id,
            "ts": format_ts(ts),
            "seq": self._next_seq(),
            "data": data,
        }
        topic = f"{TOPIC_PREFIX}/{self._org}/{self._site}/{self._machine_id}/{kind}"
        return OutgoingMessage(topic, json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())

    def telemetry(self, ts: datetime, data: dict[str, Any]) -> OutgoingMessage:
        return self._envelope("telemetry.v1", "telemetry", ts, data)

    def event(self, ts: datetime, event_type: str, **fields: str | None) -> OutgoingMessage:
        data: dict[str, Any] = {"type": event_type}
        data.update({k: v for k, v in fields.items() if v is not None})
        return self._envelope("event.v1", "event", ts, data)


def heartbeat(org: str, site: str, collector_id: str, ts: datetime, seq: int, data: dict[str, Any]) -> OutgoingMessage:
    body = {
        "schema": "heartbeat.v1",
        "msg_id": uuid7(),
        "collector_id": collector_id,
        "ts": format_ts(ts),
        "seq": seq,
        "data": data,
    }
    topic = f"{TOPIC_PREFIX}/{org}/{site}/collector/{collector_id}/heartbeat"
    return OutgoingMessage(topic, json.dumps(body, separators=(",", ":")).encode(), retain=True, buffered=False)
