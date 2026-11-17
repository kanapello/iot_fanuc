from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from conftest import FakeClock
from jsonschema import Draft202012Validator

from collector.drivers.base import Alarm, DriverConnectionError, MachineSnapshot
from collector.messages import MessageFactory, OutgoingMessage, uuid7
from collector.poller import MachinePoller


class FakeDriver:
    def __init__(self) -> None:
        self.snapshot = MachineSnapshot(mode="MEM", run="START", motion=True, alarm=False, program="O1", part_count=1)
        self.alarms: list[Alarm] = []
        self.reachable = True

    def connect(self) -> None:
        if not self.reachable:
            raise DriverConnectionError("down")

    def disconnect(self) -> None:
        pass

    def read_snapshot(self) -> MachineSnapshot:
        if not self.reachable:
            raise DriverConnectionError("down")
        return self.snapshot

    def read_alarms(self) -> list[Alarm]:
        return self.alarms


class ListPublisher:
    def __init__(self) -> None:
        self.messages: list[OutgoingMessage] = []

    def publish(self, message: OutgoingMessage) -> None:
        self.messages.append(message)

    def bodies(self, kind: str) -> list[dict[str, Any]]:
        return [json.loads(m.payload) for m in self.messages if m.topic.endswith("/" + kind)]


class Harness:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.driver = FakeDriver()
        self.publisher = ListPublisher()
        self.start = datetime(2026, 11, 16, 19, 0, 0, tzinfo=UTC)
        self.poller = MachinePoller(
            "m07",
            self.driver,
            MessageFactory("demo", "plant1", "edge1", "m07"),
            self.publisher,
            keyframe_interval_s=10,
            now=lambda: self.start + timedelta(seconds=clock.value - 1000),
            monotonic=clock,
        )

    def tick(self, seconds: float = 1.0) -> None:
        self.clock.advance(seconds)
        self.poller.poll_once()


def test_publishes_on_change_and_keyframe(clock: FakeClock) -> None:
    h = Harness(clock)
    h.poller.poll_once()
    for _ in range(9):
        h.tick()
    assert len(h.publisher.bodies("telemetry")) == 1, "unchanged data must not be republished"
    h.tick()  # 10 s since the last publish -> keyframe
    assert len(h.publisher.bodies("telemetry")) == 2
    h.driver.snapshot = MachineSnapshot(mode="MEM", run="START", motion=True, alarm=False, program="O1", part_count=2)
    h.tick()
    telemetry = h.publisher.bodies("telemetry")
    assert len(telemetry) == 3 and telemetry[-1]["data"]["part_count"] == 2
    assert [m["seq"] for m in telemetry] == [1, 2, 3]
    assert telemetry[0]["ts"] == "2026-11-16T19:00:00.000Z"
    assert h.publisher.messages[0].topic == "mm/v1/demo/plant1/m07/telemetry"


def test_alarm_and_program_events(clock: FakeClock) -> None:
    h = Harness(clock)
    h.poller.poll_once()
    h.driver.snapshot = MachineSnapshot(mode="MEM", run="STOP", alarm=True, program="O1")
    h.driver.alarms = [Alarm("SV0401", "V-READY OFF", "servo")]
    h.tick()
    h.tick()  # same alarm still active -> no duplicate event
    h.driver.snapshot = MachineSnapshot(mode="MEM", run="STOP", alarm=False, program="O2")
    h.driver.alarms = []
    h.tick()
    events = [(e["data"]["type"], e["data"].get("code")) for e in h.publisher.bodies("event")]
    assert events == [("alarm_raised", "SV0401"), ("alarm_cleared", "SV0401"), ("program_changed", "O2")]


def test_connection_loss_and_backoff(clock: FakeClock) -> None:
    h = Harness(clock)
    h.poller.poll_once()
    h.driver.reachable = False
    h.tick()
    assert [e["data"]["type"] for e in h.publisher.bodies("event")] == ["connection_lost"]
    assert h.publisher.bodies("telemetry")[-1]["data"] == {"connected": False}
    for _ in range(25):
        h.tick()
    offline = [t for t in h.publisher.bodies("telemetry") if t["data"] == {"connected": False}]
    assert len(offline) == 3, "offline state is repeated only as a keyframe"
    h.driver.reachable = True
    for _ in range(31):  # backoff is capped at 30 s
        h.tick()
    assert h.poller.connected
    assert [e["data"]["type"] for e in h.publisher.bodies("event")] == ["connection_lost", "connection_restored"]
    seqs = [json.loads(m.payload)["seq"] for m in h.publisher.messages]
    assert seqs == list(range(1, len(seqs) + 1)), "seq is shared by telemetry and events, without gaps"


def test_messages_match_contract(clock: FakeClock, validators: dict[str, Draft202012Validator]) -> None:
    h = Harness(clock)
    h.poller.poll_once()
    h.driver.snapshot = MachineSnapshot(mode="MEM", run="STOP", alarm=True, program="O2")
    h.driver.alarms = [Alarm("SV0401", "V-READY OFF", None)]
    h.tick()
    h.driver.reachable = False
    h.tick()
    assert len(h.publisher.messages) >= 5
    for message in h.publisher.messages:
        body = json.loads(message.payload)
        validators[body["schema"]].validate(body)


def test_uuid7_is_time_ordered() -> None:
    first, second = uuid7(1_700_000_000_000), uuid7(1_700_000_000_001)
    assert first[14] == "7" and first[19] in "89ab"
    assert first[:13] < second[:13]
