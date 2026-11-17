"""Validation of incoming MQTT messages against shared/schemas (the edge<->server contract)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry, Resource

TOPIC_ROOT = ("mm", "v1")
Kind = Literal["telemetry", "event", "heartbeat"]


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    kind: Kind
    site_id: str
    collector_id: str
    machine_id: str | None  # None for heartbeat
    msg_id: uuid.UUID
    ts: datetime
    seq: int
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Rejected:
    topic: str
    reason: str
    payload: str


@dataclass(frozen=True, slots=True)
class _Topic:
    kind: Kind
    site_id: str
    machine_id: str | None
    collector_id: str | None


def parse_topic(topic: str) -> _Topic | None:
    parts = topic.split("/")
    if tuple(parts[:2]) != TOPIC_ROOT or not all(parts):
        return None
    if len(parts) == 5 and parts[4] in ("telemetry", "event"):
        kind: Kind = "telemetry" if parts[4] == "telemetry" else "event"
        return _Topic(kind, parts[2], parts[3], None)
    if len(parts) == 6 and parts[3] == "collector" and parts[5] == "heartbeat":
        return _Topic("heartbeat", parts[2], None, parts[4])
    return None


class MessageValidator:
    """Loads every ``<name>.v<N>.json`` schema from a directory and validates messages."""

    MAX_STORED_PAYLOAD = 8192

    def __init__(self, schemas_dir: Path) -> None:
        resources: list[tuple[str, Resource[Any]]] = []
        schemas: dict[str, Any] = {}
        for path in sorted(schemas_dir.glob("*.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[path.name.removesuffix(".json")] = schema
            resource = Resource.from_contents(schema)
            resources += [(schema["$id"], resource), (path.name, resource)]
        if not schemas:
            raise RuntimeError(f"no message schemas found in {schemas_dir}")
        registry: Registry[Any] = Registry().with_resources(resources)
        self._validators = {name: Draft202012Validator(schema, registry=registry) for name, schema in schemas.items()}

    def parse(self, topic: str, payload: bytes) -> ParsedMessage | Rejected:
        def reject(reason: str) -> Rejected:
            return Rejected(topic, reason, payload[: self.MAX_STORED_PAYLOAD].decode("utf-8", errors="replace"))

        parsed_topic = parse_topic(topic)
        if parsed_topic is None:
            return reject("unknown topic")
        try:
            body = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return reject("payload is not valid JSON")
        if not isinstance(body, dict):
            return reject("payload is not a JSON object")

        schema_name = body.get("schema")
        if not isinstance(schema_name, str) or schema_name.split(".")[0] != parsed_topic.kind:
            return reject(f"schema {schema_name!r} does not match topic kind '{parsed_topic.kind}'")
        validator = self._validators.get(schema_name)
        if validator is None:
            return reject(f"unsupported schema {schema_name!r}")
        error = best_match(validator.iter_errors(body))
        if error is not None:
            location = "/".join(str(p) for p in error.absolute_path) or "<root>"
            return reject(f"schema violation at {location}: {error.message}"[:500])

        machine_id = body.get("machine_id")
        if parsed_topic.kind == "heartbeat":
            machine_id = None
            if body["collector_id"] != parsed_topic.collector_id:
                return reject("collector_id does not match topic")
        elif machine_id != parsed_topic.machine_id:
            return reject("machine_id does not match topic")

        try:
            ts = datetime.fromisoformat(body["ts"])
        except ValueError:
            return reject("ts is not a valid timestamp")
        return ParsedMessage(
            kind=parsed_topic.kind,
            site_id=parsed_topic.site_id,
            collector_id=body["collector_id"],
            machine_id=machine_id,
            msg_id=uuid.UUID(body["msg_id"]),
            ts=ts,
            seq=body["seq"],
            data=body["data"],
        )
