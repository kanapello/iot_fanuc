"""Idempotent storage of raw messages."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin.service import MachineRef, record_heartbeat, register_discovered
from app.ingest.contract import MessageValidator, ParsedMessage, Rejected
from app.ingest.models import IngestReject, MachineEvent, Telemetry

log = logging.getLogger(__name__)

_TELEMETRY_SIGNALS = (
    "mode",
    "run",
    "motion",
    "emergency",
    "alarm",
    "program",
    "program_name",
    "feed_actual",
    "spindle_speed",
    "spindle_load",
    "feed_override",
    "spindle_override",
    "part_count",
)


@dataclass(frozen=True, slots=True)
class BatchResult:
    received: int = 0
    telemetry_inserted: int = 0
    events_inserted: int = 0
    heartbeats: int = 0
    duplicates: int = 0
    rejected: int = 0


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeats of the same message inside one batch (keeps the first)."""
    return list({(row["msg_id"], row["ts"]): row for row in reversed(rows)}.values())[::-1]


def _telemetry_row(msg: ParsedMessage) -> dict[str, Any]:
    row: dict[str, Any] = {
        "msg_id": msg.msg_id,
        "ts": msg.ts,
        "machine_id": msg.machine_id,
        "collector_id": msg.collector_id,
        "seq": msg.seq,
        "connected": msg.data["connected"],
    }
    row.update({name: msg.data.get(name) for name in _TELEMETRY_SIGNALS})
    return row


def _event_row(msg: ParsedMessage) -> dict[str, Any]:
    return {
        "msg_id": msg.msg_id,
        "ts": msg.ts,
        "machine_id": msg.machine_id,
        "collector_id": msg.collector_id,
        "seq": msg.seq,
        "type": msg.data["type"],
        "code": msg.data.get("code"),
        "message": msg.data.get("message"),
        "payload": msg.data,
    }


class IngestService:
    """Validates a batch of MQTT messages and stores it in one transaction.

    Storing is idempotent (``ON CONFLICT DO NOTHING`` on the message key), so a
    batch can be retried and messages can be redelivered any number of times.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], validator: MessageValidator) -> None:
        self._session_factory = session_factory
        self._validator = validator
        self._known_machines: set[MachineRef] = set()

    async def handle_batch(self, messages: Sequence[tuple[str, bytes]]) -> BatchResult:
        telemetry: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        heartbeats: list[ParsedMessage] = []
        rejects: list[Rejected] = []
        refs: set[MachineRef] = set()

        for topic, payload in messages:
            parsed = self._validator.parse(topic, payload)
            if isinstance(parsed, Rejected):
                rejects.append(parsed)
                continue
            if parsed.kind == "heartbeat":
                heartbeats.append(parsed)
                continue
            assert parsed.machine_id is not None
            refs.add(MachineRef(parsed.site_id, parsed.machine_id, parsed.collector_id))
            (telemetry if parsed.kind == "telemetry" else events).append(
                _telemetry_row(parsed) if parsed.kind == "telemetry" else _event_row(parsed)
            )

        telemetry, events = _dedupe(telemetry), _dedupe(events)
        new_refs = refs - self._known_machines
        telemetry_inserted = events_inserted = 0

        async with self._session_factory() as session, session.begin():
            await register_discovered(session, new_refs)
            if telemetry:
                result = await session.execute(
                    insert(Telemetry).values(telemetry).on_conflict_do_nothing(index_elements=["msg_id", "ts"])
                )
                telemetry_inserted = result.rowcount  # type: ignore[attr-defined]
            if events:
                result = await session.execute(
                    insert(MachineEvent).values(events).on_conflict_do_nothing(index_elements=["msg_id", "ts"])
                )
                events_inserted = result.rowcount  # type: ignore[attr-defined]
            for hb in sorted(heartbeats, key=lambda m: m.ts):
                await record_heartbeat(session, hb.site_id, hb.collector_id, hb.ts, hb.data.get("version"))
            if rejects:
                await session.execute(
                    insert(IngestReject).values(
                        [{"topic": r.topic, "reason": r.reason, "payload": r.payload} for r in rejects]
                    )
                )
        self._known_machines |= new_refs

        for reject in rejects:
            log.warning("message rejected", extra={"topic": reject.topic, "reason": reject.reason})
        accepted = len(messages) - len(rejects) - len(heartbeats)
        return BatchResult(
            received=len(messages),
            telemetry_inserted=telemetry_inserted,
            events_inserted=events_inserted,
            heartbeats=len(heartbeats),
            duplicates=accepted - telemetry_inserted - events_inserted,
            rejected=len(rejects),
        )
