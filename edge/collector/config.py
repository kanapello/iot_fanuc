"""Collector configuration (YAML -> pydantic)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]

# What the collector reads from a machine. The minimum keeps the timeline and OEE working;
# feed, spindle speed and spindle load are read only when listed explicitly (ADR 0007).
Signal = Literal["status", "program", "part_count", "alarms", "feed_actual", "spindle_speed", "spindle_load"]
MINIMAL_SIGNALS: tuple[Signal, ...] = ("status", "program", "part_count", "alarms")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MqttConfig(_Model):
    host: str
    port: int = 1883
    tls: bool = False
    username: str | None = None
    password_env: str | None = None
    keepalive_s: int = 30


class FocasConfig(_Model):
    library_path: str


class BufferConfig(_Model):
    """Store-and-forward buffer settings."""

    path: str = "./data/buffer.db"
    # Not longer than 7 days: the server compresses telemetry after 30 days and late
    # data must never reach a compressed chunk (ARCHITECTURE.md 3.6, 5.3).
    max_age_days: int = Field(default=7, ge=1, le=7)


class PartCounterConfig(_Model):
    source: Literal["macro", "param"] = "macro"
    address: int = 3901


class _MachineBase(_Model):
    id: Identifier
    name: str | None = None
    poll_interval_ms: int = Field(default=1000, ge=100)
    signals: list[Signal] = Field(default_factory=lambda: list(MINIMAL_SIGNALS))

    @field_validator("signals")
    @classmethod
    def _status_is_always_read(cls, value: list[Signal]) -> list[Signal]:
        if "status" not in value:
            raise ValueError("'status' (cnc_statinfo) is always read: the timeline depends on it")
        return list(dict.fromkeys(value))


class FanucFocasMachine(_MachineBase):
    driver: Literal["fanuc_focas"]
    ip: str
    port: int = 8193
    # 1 s by default; 2-5 s for a control that answers slowly. Faster is not allowed.
    poll_interval_ms: int = Field(default=1000, ge=1000, le=5000)
    part_counter: PartCounterConfig = PartCounterConfig()


class SimulatorMachine(_MachineBase):
    driver: Literal["simulator"]
    profile: str = "milling_normal"
    offset_s: float = 0.0
    time_scale: float = Field(default=1.0, gt=0)
    # Demo history: on the very first start replay this many past hours with historical timestamps.
    backfill_hours: float = Field(default=0.0, ge=0, le=72)
    backfill_step_s: float = Field(default=5.0, gt=0)


MachineConfig = Annotated[FanucFocasMachine | SimulatorMachine, Field(discriminator="driver")]


class CollectorConfig(_Model):
    org: Identifier  # organization code
    site: Identifier  # site code inside the organization
    collector_id: Identifier
    mqtt: MqttConfig
    focas: FocasConfig | None = None
    buffer: BufferConfig = BufferConfig()
    keyframe_interval_s: float = Field(default=10.0, gt=0)
    scenarios_dir: str | None = None
    machines: list[MachineConfig] = Field(min_length=1)


def load_config(path: str | Path) -> CollectorConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return CollectorConfig.model_validate(raw)
