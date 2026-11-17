"""Raw, immutable data owned by the ingest module (INSERT only)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Telemetry(Base):
    """TimescaleDB hypertable partitioned by ``ts``.

    The idempotency key is (msg_id, ts): a unique index on a hypertable must
    contain the partitioning column, and a redelivered message carries the
    same ``ts`` (see docs/adr/0001).
    """

    __tablename__ = "telemetry"
    __table_args__ = (Index("ix_telemetry_machine_id_ts", "machine_id", "ts"),)

    msg_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    machine_id: Mapped[int] = mapped_column(BigInteger)
    collector_id: Mapped[str] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(BigInteger)
    connected: Mapped[bool] = mapped_column(Boolean)
    mode: Mapped[str | None] = mapped_column(Text)
    run: Mapped[str | None] = mapped_column(Text)
    aut_code: Mapped[int | None] = mapped_column(Integer)
    run_code: Mapped[int | None] = mapped_column(Integer)
    alarm_code: Mapped[int | None] = mapped_column(Integer)
    emergency_code: Mapped[int | None] = mapped_column(Integer)
    motion: Mapped[bool | None] = mapped_column(Boolean)
    emergency: Mapped[bool | None] = mapped_column(Boolean)
    alarm: Mapped[bool | None] = mapped_column(Boolean)
    program: Mapped[str | None] = mapped_column(Text)
    program_name: Mapped[str | None] = mapped_column(Text)
    feed_actual: Mapped[float | None] = mapped_column(Float)
    spindle_speed: Mapped[float | None] = mapped_column(Float)
    spindle_load: Mapped[float | None] = mapped_column(Float)
    feed_override: Mapped[float | None] = mapped_column(Float)
    spindle_override: Mapped[float | None] = mapped_column(Float)
    part_count: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MachineEvent(Base):
    __tablename__ = "machine_events"
    __table_args__ = (Index("ix_machine_events_machine_id_ts", "machine_id", "ts"),)

    msg_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    machine_id: Mapped[int] = mapped_column(BigInteger)
    collector_id: Mapped[str] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(Text)
    code: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestReject(Base):
    """Messages that failed contract validation, kept for diagnostics."""

    __tablename__ = "ingest_rejects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    topic: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text)
