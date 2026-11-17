"""Collector heartbeat (version, uptime, buffer size, machine connections)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from collector.buffer import Buffer
from collector.messages import heartbeat
from collector.publisher import Publisher

VERSION = "0.2.0"
HEARTBEAT_INTERVAL_S = 15.0


def run_heartbeat(
    site: str,
    collector_id: str,
    publisher: Publisher,
    buffer: Buffer,
    machines: Callable[[], dict[str, bool]],
    stop: threading.Event,
) -> None:
    started = time.monotonic()
    while not stop.is_set():
        data = {
            "version": VERSION,
            "uptime_s": round(time.monotonic() - started, 1),
            "buffer_size": buffer.size(),
            "machines": machines(),
        }
        seq = buffer.next_seq(f"collector:{collector_id}")
        publisher.publish(heartbeat(site, collector_id, datetime.now(UTC), seq, data))
        stop.wait(HEARTBEAT_INTERVAL_S)
