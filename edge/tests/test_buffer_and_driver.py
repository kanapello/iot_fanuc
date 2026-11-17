from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from collector.buffer import Buffer
from collector.config import BufferConfig, PartCounterConfig
from collector.drivers.base import DriverConnectionError
from collector.drivers.fanuc_focas import FanucFocasDriver

HERE = Path(__file__).parent


def test_buffer_keeps_messages_and_seq_across_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "data" / "buffer.db")
    buffer = Buffer(path)
    epoch = buffer.epoch
    assert [buffer.next_seq("m07"), buffer.next_seq("m07"), buffer.next_seq("m08")] == [1, 2, 1]
    first = buffer.add("t/1", b"one")
    buffer.add("t/2", b"two", retain=True)
    buffer.delete(first)  # PUBACK received for the first message only
    buffer.close()

    reopened = Buffer(path)
    assert reopened.epoch == epoch  # the same buffer file keeps its epoch
    assert reopened.next_seq("m07") == 3
    pending = reopened.pending()
    assert [(m.topic, m.payload, m.retain) for m in pending] == [("t/2", b"two", True)]
    assert reopened.pending(after_id=pending[0].id) == []
    assert reopened.size() == 1

    lost = Buffer(str(tmp_path / "data" / "new-buffer.db"))  # the buffer file was lost and recreated
    assert lost.epoch != epoch and lost.next_seq("m07") == 1


def test_focas_driver_against_fake_library(tmp_path: Path) -> None:
    compiler = shutil.which("gcc") or shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler not available")
    lib = tmp_path / "libfakefwlib.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-o", str(lib), str(HERE / "fake_fwlib.c")], check=True)

    driver = FanucFocasDriver(str(lib), "10.0.0.1")
    driver.connect()
    snap = driver.read_snapshot()
    assert (snap.mode, snap.aut_code, snap.run, snap.run_code) == ("MEM", 1, "START", 3)
    assert (snap.alarm, snap.alarm_code, snap.emergency, snap.emergency_code) == (False, 0, False, 0)
    assert (snap.program, snap.program_name, snap.part_count) == ("O1234", "O1234", 15873)
    assert (snap.feed_actual, snap.spindle_speed, snap.spindle_load) == (1250.0, 8000.0, 37.5)
    driver.disconnect()

    alarmed = FanucFocasDriver(str(lib), "10.0.0.2", part_counter=PartCounterConfig(source="param", address=6711))
    alarmed.connect()
    snap = alarmed.read_snapshot()
    assert (snap.alarm, snap.alarm_code, snap.run) == (True, 1, "STOP")
    assert (snap.program_name, snap.spindle_load, snap.part_count) == ("O1234", None, 15873)
    assert [(a.code, a.message) for a in alarmed.read_alarms()] == [("SV0401", "V-READY OFF")]
    alarmed.disconnect()

    with pytest.raises(DriverConnectionError):
        FanucFocasDriver(str(lib), "10.9.9.9").connect()


def test_buffer_age_is_limited_to_seven_days() -> None:
    assert BufferConfig().max_age_days == 7
    with pytest.raises(ValueError):
        BufferConfig(max_age_days=8)
