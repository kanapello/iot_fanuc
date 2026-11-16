"""MQTT consumer: broker -> batches -> IngestService."""

from __future__ import annotations

import asyncio
import logging
import ssl
from dataclasses import dataclass

import aiomqtt
from paho.mqtt.client import Client as PahoClient
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from app.core.config import Settings
from app.ingest.service import IngestService

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 3.0
RETRY_DELAY_MAX_S = 30.0


@dataclass(slots=True)
class _Stats:
    received: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0


def _paho(client: aiomqtt.Client) -> PahoClient:
    # aiomqtt has no public API for manual acknowledgements, paho does.
    return client._client


def subscription_topic(settings: Settings) -> str:
    topic = "mm/v1/#"
    return f"$share/{settings.ingest_share_group}/{topic}" if settings.ingest_share_group else topic


async def _next_batch(client: aiomqtt.Client, limit: int) -> list[aiomqtt.Message]:
    """Wait for one message, then take everything already queued (up to ``limit``)."""
    batch = [await anext(client.messages)]
    queue = client._queue
    while len(batch) < limit:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return batch


async def _store_with_retry(service: IngestService, batch: list[aiomqtt.Message], stats: _Stats) -> None:
    payloads = [(str(m.topic), bytes(m.payload) if isinstance(m.payload, bytes | bytearray) else b"") for m in batch]
    delay = 1.0
    while True:
        try:
            result = await service.handle_batch(payloads)
            break
        except Exception:
            # Nothing is acknowledged yet: keep the batch and retry, the storage is idempotent.
            log.exception("failed to store batch, retrying", extra={"size": len(batch), "retry_in_s": delay})
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_DELAY_MAX_S)
    stats.received += result.received
    stats.stored += result.telemetry_inserted + result.events_inserted + result.heartbeats
    stats.duplicates += result.duplicates
    stats.rejected += result.rejected


async def _report(stats: _Stats, interval_s: float = 30.0) -> None:
    while True:
        await asyncio.sleep(interval_s)
        log.info(
            "ingest stats",
            extra={
                "received": stats.received,
                "stored": stats.stored,
                "duplicates": stats.duplicates,
                "rejected": stats.rejected,
            },
        )


async def consume(settings: Settings, service: IngestService) -> None:
    """Connect, subscribe and store messages; a message is acknowledged only after commit."""
    connect_props = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]
    connect_props.SessionExpiryInterval = settings.ingest_session_expiry_s
    client = aiomqtt.Client(
        settings.mqtt_host,
        settings.mqtt_port,
        username=settings.mqtt_username,
        password=settings.mqtt_password,
        identifier=settings.ingest_client_id,
        protocol=aiomqtt.ProtocolVersion.V5,
        clean_start=False,
        properties=connect_props,
        tls_context=ssl.create_default_context() if settings.mqtt_tls else None,
    )
    _paho(client).manual_ack_set(True)
    stats = _Stats()
    async with client:
        topic = subscription_topic(settings)
        await client.subscribe(topic, qos=1)
        log.info("ingest subscribed", extra={"topic": topic, "client_id": settings.ingest_client_id})
        reporter = asyncio.create_task(_report(stats))
        if settings.ingest_ready_file is not None:
            settings.ingest_ready_file.touch()
        try:
            while True:
                batch = await _next_batch(client, settings.ingest_batch_size)
                await _store_with_retry(service, batch, stats)
                for message in batch:
                    if message.qos > 0 and message.mid:
                        _paho(client).ack(message.mid, message.qos)
        finally:
            reporter.cancel()
            if settings.ingest_ready_file is not None:
                settings.ingest_ready_file.unlink(missing_ok=True)


async def run_forever(settings: Settings, service: IngestService) -> None:
    while True:
        try:
            await consume(settings, service)
        except aiomqtt.MqttError as exc:
            log.warning(
                "MQTT connection lost, reconnecting", extra={"error": str(exc), "retry_in_s": RECONNECT_DELAY_S}
            )
            await asyncio.sleep(RECONNECT_DELAY_S)
