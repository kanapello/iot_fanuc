"""Driver interface shared by all controller types."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import asdict, dataclass
from typing import Any, Protocol

# Snapshot fields that belong to each configurable signal (collector.config.Signal).
# Overrides are not read by the collector at all for now: they are never published.
SIGNAL_FIELDS: dict[str, tuple[str, ...]] = {
    "status": ("mode", "aut_code", "run_code", "alarm_code", "emergency_code", "run", "motion", "emergency", "alarm"),
    "program": ("program", "program_name"),
    "part_count": ("part_count",),
    "alarms": (),
    "feed_actual": ("feed_actual",),
    "spindle_speed": ("spindle_speed",),
    "spindle_load": ("spindle_load",),
}


def published_fields(signals: Collection[str]) -> frozenset[str]:
    """Telemetry fields of the configured signals (plus ``connected``)."""
    return frozenset({"connected"}).union(*(SIGNAL_FIELDS[s] for s in signals))


class DriverError(Exception):
    """A read failed but the connection is believed to be alive."""


class DriverConnectionError(DriverError):
    """The machine is unreachable; the poller must reconnect."""


@dataclass(frozen=True, slots=True)
class MachineSnapshot:
    """All signals of one poll. ``None`` = signal not supported / not available."""

    mode: str | None = None
    aut_code: int | None = None
    run_code: int | None = None
    alarm_code: int | None = None  # alarm = (alarm_code == 1)
    emergency_code: int | None = None  # emergency = (emergency_code == 1)
    run: str | None = None  # RESET | STOP | HOLD | START | MSTR
    motion: bool | None = None
    emergency: bool | None = None
    alarm: bool | None = None
    program: str | None = None
    program_name: str | None = None
    feed_actual: float | None = None
    spindle_speed: float | None = None
    spindle_load: float | None = None
    feed_override: float | None = None
    spindle_override: float | None = None
    part_count: int | None = None

    def to_data(self) -> dict[str, Any]:
        return {"connected": True, **asdict(self)}


@dataclass(frozen=True, slots=True)
class Alarm:
    code: str
    message: str
    category: str | None = None


class MachineDriver(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read_snapshot(self) -> MachineSnapshot: ...

    def read_alarms(self) -> list[Alarm]: ...
