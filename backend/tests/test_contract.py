from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ingest.contract import MessageValidator, ParsedMessage, Rejected, parse_topic
from tests.conftest import encode, make_message


def test_valid_telemetry(validator: MessageValidator) -> None:
    body = make_message("telemetry")
    parsed = validator.parse(*encode(body))
    assert isinstance(parsed, ParsedMessage)
    assert (parsed.kind, parsed.org_code, parsed.site_code, parsed.machine_code) == ("telemetry", "torg", "t1", "m1")
    assert parsed.ts == datetime(2026, 11, 16, 19, 1, 2, 345000, tzinfo=UTC)
    assert str(parsed.msg_id) == body["msg_id"]


def test_disconnected_telemetry_needs_only_connected(validator: MessageValidator) -> None:
    parsed = validator.parse(*encode(make_message("telemetry", data={"connected": False})))
    assert isinstance(parsed, ParsedMessage)


def test_valid_event_and_heartbeat(validator: MessageValidator) -> None:
    event = validator.parse(*encode(make_message("event")))
    heartbeat = validator.parse(*encode(make_message("heartbeat")))
    assert isinstance(event, ParsedMessage) and event.kind == "event"
    assert isinstance(heartbeat, ParsedMessage) and heartbeat.machine_code is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ts": "2026-11-16 19:01:02"}, "schema violation at ts"),
        ({"ts": "2026-11-16T19:01:02+03:00"}, "schema violation at ts"),
        ({"msg_id": "not-a-uuid"}, "schema violation at msg_id"),
        ({"seq": -1}, "schema violation at seq"),
        ({"schema": "telemetry.v9"}, "unsupported schema"),
        ({"schema": "event.v1"}, "does not match topic kind"),
        ({"data": {"mode": "MEM"}}, "schema violation at data"),
        ({"data": {"connected": True, "run": "RUNNING"}}, "schema violation at data/run"),
        ({"data": {"connected": True, "unknown_signal": 1}}, "schema violation at data"),
    ],
)
def test_invalid_telemetry_is_rejected(validator: MessageValidator, overrides: dict[str, Any], reason: str) -> None:
    body = make_message("telemetry", **overrides)
    result = validator.parse("mm/v1/torg/t1/m1/telemetry", json.dumps(body).encode())
    assert isinstance(result, Rejected)
    assert reason in result.reason


def test_topic_and_body_must_agree(validator: MessageValidator) -> None:
    body = make_message("telemetry")
    result = validator.parse("mm/v1/torg/t1/m08/telemetry", json.dumps(body).encode())
    assert isinstance(result, Rejected) and result.reason == "machine_id does not match topic"

    heartbeat = make_message("heartbeat")
    result = validator.parse("mm/v1/torg/t1/collector/other/heartbeat", json.dumps(heartbeat).encode())
    assert isinstance(result, Rejected) and result.reason == "collector_id does not match topic"


def test_garbage_is_rejected(validator: MessageValidator) -> None:
    for payload in (b"\xff\xfe", b"not json", b"[1, 2]"):
        result = validator.parse("mm/v1/torg/t1/m1/telemetry", payload)
        assert isinstance(result, Rejected)
    assert isinstance(validator.parse("mm/v1/torg/t1/m1/unknown", b"{}"), Rejected)
    assert isinstance(validator.parse("mm/v1/t1/m1/telemetry", b"{}"), Rejected)  # topic without organization


def test_parse_topic() -> None:
    assert parse_topic("mm/v2/torg/t1/m1/telemetry") is None
    assert parse_topic("mm/v1/torg/t1//telemetry") is None
    telemetry = parse_topic("mm/v1/torg/t1/m1/telemetry")
    assert telemetry is not None and (telemetry.org_code, telemetry.site_code, telemetry.machine_code) == (
        "torg",
        "t1",
        "m1",
    )
    heartbeat = parse_topic("mm/v1/torg/t1/collector/edge1/heartbeat")
    assert heartbeat is not None and (heartbeat.site_code, heartbeat.collector_id) == ("t1", "edge1")
