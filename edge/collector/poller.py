"""Polling loop of a single machine (one thread per machine, one driver handle)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from collector.drivers.base import Alarm, DriverConnectionError, DriverError, MachineDriver
from collector.messages import MessageFactory
from collector.publisher import Publisher

log = logging.getLogger(__name__)

BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MachinePoller:
    """Reads the driver, timestamps the data and hands messages to the publisher.

    No business logic: telemetry is published on change and at least once per
    ``keyframe_interval_s``; events only mirror raw transitions (alarm list,
    connection, program number).
    """

    def __init__(
        self,
        machine_id: str,
        driver: MachineDriver,
        factory: MessageFactory,
        publisher: Publisher,
        *,
        poll_interval_s: float = 1.0,
        keyframe_interval_s: float = 10.0,
        now: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.machine_id = machine_id
        self._driver = driver
        self._factory = factory
        self._publisher = publisher
        self._poll_interval_s = poll_interval_s
        self._keyframe_interval_s = keyframe_interval_s
        self._now = now
        self._monotonic = monotonic

        self._connected = False
        self._ever_connected = False
        self._backoff_s = BACKOFF_MIN_S
        self._next_connect_at = float("-inf")  # clocks may be negative (replayed history)
        self._last_data: dict[str, Any] | None = None
        self._last_publish_at = float("-inf")
        self._last_program: str | None = None
        self._active_alarms: dict[str, Alarm] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    def run(self, stop: threading.Event) -> None:
        log.info("poller started: %s", self.machine_id)
        while not stop.is_set():
            started = self._monotonic()
            try:
                self.poll_once()
            except Exception:
                log.exception("unexpected error while polling %s", self.machine_id)
            stop.wait(max(0.0, self._poll_interval_s - (self._monotonic() - started)))
        self._safe_disconnect()
        log.info("poller stopped: %s", self.machine_id)

    def poll_once(self) -> None:
        if not self._connected and not self._try_connect():
            self._publish_telemetry({"connected": False})
            return
        try:
            snapshot = self._driver.read_snapshot()
            ts = self._now()
            alarms = self._driver.read_alarms() if snapshot.alarm else []
        except DriverConnectionError as exc:
            self._on_connection_lost(exc)
            return
        except DriverError as exc:
            log.warning("%s: read failed: %s", self.machine_id, exc)
            return

        self._publish_telemetry(snapshot.to_data(), ts)
        self._publish_alarm_changes(alarms, ts)
        if snapshot.program is not None:
            if self._last_program is not None and snapshot.program != self._last_program:
                self._publisher.publish(self._factory.event(ts, "program_changed", code=snapshot.program))
            self._last_program = snapshot.program

    def _try_connect(self) -> bool:
        if self._monotonic() < self._next_connect_at:
            return False
        try:
            self._driver.connect()
        except DriverError as exc:
            log.warning("%s: connect failed (%s), retry in %.0f s", self.machine_id, exc, self._backoff_s)
            self._next_connect_at = self._monotonic() + self._backoff_s
            self._backoff_s = min(self._backoff_s * 2, BACKOFF_MAX_S)
            return False
        self._connected = True
        self._backoff_s = BACKOFF_MIN_S
        log.info("%s: connected", self.machine_id)
        if self._ever_connected:
            self._publisher.publish(self._factory.event(self._now(), "connection_restored"))
        self._ever_connected = True
        return True

    def _on_connection_lost(self, exc: Exception) -> None:
        log.warning("%s: connection lost: %s", self.machine_id, exc)
        self._connected = False
        self._safe_disconnect()
        ts = self._now()
        self._publisher.publish(self._factory.event(ts, "connection_lost", message=str(exc)))
        self._publish_telemetry({"connected": False}, ts)
        self._next_connect_at = self._monotonic() + self._backoff_s
        self._backoff_s = min(self._backoff_s * 2, BACKOFF_MAX_S)

    def _safe_disconnect(self) -> None:
        try:
            self._driver.disconnect()
        except Exception:
            log.debug("%s: disconnect failed", self.machine_id, exc_info=True)

    def _publish_telemetry(self, data: dict[str, Any], ts: datetime | None = None) -> None:
        mono = self._monotonic()
        changed = data != self._last_data
        if not changed and mono - self._last_publish_at < self._keyframe_interval_s:
            return
        self._publisher.publish(self._factory.telemetry(ts or self._now(), data))
        self._last_data = data
        self._last_publish_at = mono

    def _publish_alarm_changes(self, alarms: list[Alarm], ts: datetime) -> None:
        current = {a.code: a for a in alarms}
        for code, alarm in current.items():
            if code not in self._active_alarms:
                self._publisher.publish(
                    self._factory.event(
                        ts, "alarm_raised", code=alarm.code, message=alarm.message, category=alarm.category
                    )
                )
        for code, alarm in self._active_alarms.items():
            if code not in current:
                self._publisher.publish(
                    self._factory.event(
                        ts, "alarm_cleared", code=alarm.code, message=alarm.message, category=alarm.category
                    )
                )
        self._active_alarms = current
