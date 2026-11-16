"""Collector configuration (YAML -> pydantic)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]


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
    """Store-and-forward buffer settings. Parsed already, used from stage 2."""

    path: str = "./data/buffer.db"
    max_age_days: int = 7


class PartCounterConfig(_Model):
    source: Literal["macro", "param"] = "macro"
    address: int = 3901


class _MachineBase(_Model):
    id: Identifier
    name: str | None = None
    poll_interval_ms: int = Field(default=1000, ge=100)


class FanucFocasMachine(_MachineBase):
    driver: Literal["fanuc_focas"]
    ip: str
    port: int = 8193
    part_counter: PartCounterConfig = PartCounterConfig()


class SimulatorMachine(_MachineBase):
    driver: Literal["simulator"]
    profile: str = "milling_normal"
    offset_s: float = 0.0
    time_scale: float = Field(default=1.0, gt=0)


MachineConfig = Annotated[FanucFocasMachine | SimulatorMachine, Field(discriminator="driver")]


class CollectorConfig(_Model):
    site: Identifier
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
