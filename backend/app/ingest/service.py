"""Idempotent storage of raw messages."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin.service import CollectorRef, collector_directory, record_heartbeat, register_machines
from app.ingest.contract import MessageValidator, ParsedMessage, Rejected
from app.ingest.models import IngestReject, MachineEvent, Telemetry
from app.timeline.service import mark_dirty

log = logging.getLogger(__name__)

_TELEMETRY_SIGNALS = (
    "mode",
    "run",
    "aut_code",
    "run_code",
    "alarm_code",
    "emergency_code",
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


def _telemetry_row(msg: ParsedMessage, machine_id: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "msg_id": msg.msg_id,
        "ts": msg.ts,
        "machine_id": machine_id,
        "collector_id": msg.collector_id,
        "seq": msg.seq,
        "connected": msg.data["connected"],
    }
    row.update({name: msg.data.get(name) for name in _TELEMETRY_SIGNALS})
    return row


def _event_row(msg: ParsedMessage, machine_id: int) -> dict[str, Any]:
    return {
        "msg_id": msg.msg_id,
        "ts": msg.ts,
        "machine_id": machine_id,
        "collector_id": msg.collector_id,
        "seq": msg.seq,
        "type": msg.data["type"],
        "code": msg.data.get("code"),
        "message": msg.data.get("message"),
        "payload": msg.data,
    }


class IngestService:
    """Validates a batch of MQTT messages and stores it in one transaction.

    A message is accepted only from a registered collector and only into the
    organization and site that collector belongs to. Storing is idempotent
    (``ON CONFLICT DO NOTHING`` on the message key), so a batch can be retried
    and messages can be redelivered any number of times.

    Collectors and machines are cached and reloaded every ``refresh_s`` seconds
    (and at once when an unknown collector appears): a collector moved to another
    site or deleted stops writing into its old place within that time.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        validator: MessageValidator,
        refresh_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._validator = validator
        self._refresh_s = refresh_s
        self._clock = clock
        self._loaded_at: float | None = None
        self._collectors: dict[str, CollectorRef] = {}
        self._machines: dict[tuple[int, str], int] = {}

    async def _refresh_directories(self, session: AsyncSession, collector_ids: set[str]) -> None:
        """Reload the collectors (and forget the machines) when stale or when a collector is unknown."""
        now = self._clock()
        stale = self._loaded_at is None or now - self._loaded_at >= self._refresh_s
        if stale or collector_ids - self._collectors.keys():
            self._collectors = await collector_directory(session)
            self._machines = {}  # looked up again (register_machines) on first use
            self._loaded_at = now

    def _check_origin(self, msg: ParsedMessage) -> str | None:
        """Reason to reject, if the collector may not write to the topic's site."""
        collector = self._collectors.get(msg.collector_id)
        if collector is None:
            return "unknown collector"
        if (collector.org_code, collector.site_code) != (msg.org_code, msg.site_code):
            return "collector does not belong to the organization/site of the topic"
        return None

    async def handle_batch(self, messages: Sequence[tuple[str, bytes]]) -> BatchResult:
        accepted: list[ParsedMessage] = []
        heartbeats: list[ParsedMessage] = []
        rejects: list[Rejected] = []
        parsed_messages = [(topic, payload, self._validator.parse(topic, payload)) for topic, payload in messages]

        async with self._session_factory() as session, session.begin():
            await self._refresh_directories(
                session, {p.collector_id for _, _, p in parsed_messages if isinstance(p, ParsedMessage)}
            )

            for topic, payload, parsed in parsed_messages:
                if isinstance(parsed, Rejected):
                    rejects.append(parsed)
                    continue
                reason = self._check_origin(parsed)
                if reason is not None:
                    stored = payload[: self._validator.MAX_STORED_PAYLOAD].decode("utf-8", errors="replace")
                    rejects.append(Rejected(topic, reason, stored))
                elif parsed.kind == "heartbeat":
                    heartbeats.append(parsed)
                else:
                    accepted.append(parsed)

            # machines are created only inside the site of their own collector
            keys = {(self._collectors[m.collector_id].site_id, m.machine_code or "", m.collector_id) for m in accepted}
            new_keys = [k for k in keys if (k[0], k[1]) not in self._machines]
            registered = await register_machines(session, new_keys)

            def machine_id(msg: ParsedMessage) -> int:
                key = (self._collectors[msg.collector_id].site_id, msg.machine_code or "")
                return registered.get(key) or self._machines[key]

            telemetry = _dedupe([_telemetry_row(m, machine_id(m)) for m in accepted if m.kind == "telemetry"])
            events = _dedupe([_event_row(m, machine_id(m)) for m in accepted if m.kind == "event"])
            telemetry_inserted = events_inserted = 0
            if telemetry:
                result = await session.execute(
                    insert(Telemetry).values(telemetry).on_conflict_do_nothing(index_elements=["msg_id", "ts"])
                )
                telemetry_inserted = result.rowcount  # type: ignore[attr-defined]
                # ingest derives nothing: it only tells the worker which period changed
                dirty: dict[int, datetime] = {}
                for row in telemetry:
                    dirty[row["machine_id"]] = min(row["ts"], dirty.get(row["machine_id"], row["ts"]))
                await mark_dirty(session, dirty)
            if events:
                result = await session.execute(
                    insert(MachineEvent).values(events).on_conflict_do_nothing(index_elements=["msg_id", "ts"])
                )
                events_inserted = result.rowcount  # type: ignore[attr-defined]
            lost_buffers = []
            for hb in sorted(heartbeats, key=lambda m: m.ts):
                if await record_heartbeat(
                    session, hb.collector_id, hb.ts, hb.data.get("version"), hb.data.get("epoch")
                ):
                    lost_buffers.append(hb)
            if rejects:
                await session.execute(
                    insert(IngestReject).values(
                        [{"topic": r.topic, "reason": r.reason, "payload": r.payload} for r in rejects]
                    )
                )
        self._machines.update(registered)

        for hb in lost_buffers:
            log.warning(
                "collector buffer lost, seq restarted",
                extra={"collector_id": hb.collector_id, "epoch": hb.data.get("epoch"), "at": hb.ts.isoformat()},
            )
        for reject in rejects:
            log.warning("message rejected", extra={"topic": reject.topic, "reason": reject.reason})
        return BatchResult(
            received=len(messages),
            telemetry_inserted=telemetry_inserted,
            events_inserted=events_inserted,
            heartbeats=len(heartbeats),
            duplicates=len(accepted) - telemetry_inserted - events_inserted,
            rejected=len(rejects),
        )
