from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from collector.buffer import Buffer
from collector.config import BufferConfig, FanucFocasMachine, PartCounterConfig
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


@pytest.fixture
def fake_lib(tmp_path: Path) -> Callable[[str], int]:
    """A freshly built fake Fwlib; returns a counter of calls per FOCAS function."""
    compiler = shutil.which("gcc") or shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler not available")
    lib_path = tmp_path / "libfakefwlib.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-o", str(lib_path), str(HERE / "fake_fwlib.c")], check=True)
    lib = ctypes.CDLL(str(lib_path))
    lib.fake_calls.restype = ctypes.c_int
    lib.fake_calls.argtypes = [ctypes.c_char_p]
    calls: Any = lambda name: int(lib.fake_calls(name.encode()))  # noqa: E731
    calls.path = str(lib_path)
    return cast(Callable[[str], int], calls)


def test_focas_driver_reads_only_the_minimum_by_default(fake_lib: Any) -> None:
    """Default config: status, program, part counter, alarms. No feed, spindle speed or load calls."""
    driver = FanucFocasDriver(fake_lib.path, "10.0.0.1")
    driver.connect()
    snap = driver.read_snapshot()
    assert (snap.mode, snap.aut_code, snap.run, snap.run_code) == ("MEM", 1, "START", 3)
    assert (snap.alarm, snap.alarm_code, snap.emergency, snap.emergency_code) == (False, 0, False, 0)
    assert (snap.program, snap.program_name, snap.part_count) == ("O1234", "O1234", 15873)
    assert (snap.feed_actual, snap.spindle_speed, snap.spindle_load) == (None, None, None)
    assert [fake_lib(name) for name in ("cnc_actf", "cnc_acts", "cnc_rdspmeter")] == [0, 0, 0]
    assert [fake_lib(name) for name in ("cnc_statinfo", "cnc_rdprgnum", "cnc_exeprgname", "cnc_rdmacro")] == [
        1,
        1,
        1,
        1,
    ]
    driver.disconnect()

    alarmed = FanucFocasDriver(fake_lib.path, "10.0.0.2", part_counter=PartCounterConfig(source="param", address=6711))
    alarmed.connect()
    snap = alarmed.read_snapshot()
    assert (snap.alarm, snap.alarm_code, snap.run) == (True, 1, "STOP")
    assert (snap.program_name, snap.part_count) == ("O1234", 15873)
    assert [(a.code, a.message) for a in alarmed.read_alarms()] == [("SV0401", "V-READY OFF")]
    alarmed.disconnect()

    with pytest.raises(DriverConnectionError):
        FanucFocasDriver(fake_lib.path, "10.9.9.9").connect()


def test_focas_driver_reads_feed_speed_and_load_only_when_enabled(fake_lib: Any) -> None:
    signals = ["status", "program", "part_count", "alarms", "feed_actual", "spindle_speed", "spindle_load"]
    driver = FanucFocasDriver(fake_lib.path, "10.0.0.1", signals=signals)
    driver.connect()
    snap = driver.read_snapshot()
    assert (snap.feed_actual, snap.spindle_speed, snap.spindle_load) == (1250.0, 8000.0, 37.5)
    assert [fake_lib(name) for name in ("cnc_actf", "cnc_acts", "cnc_rdspmeter")] == [1, 1, 1]
    # only the status: one call per poll
    lean = FanucFocasDriver(fake_lib.path, "10.0.0.1", signals=["status"])
    lean.connect()
    lean.read_snapshot()
    assert (fake_lib("cnc_statinfo"), fake_lib("cnc_rdprgnum"), fake_lib("cnc_rdmacro")) == (2, 1, 1)


def test_machine_config_defaults_to_the_minimum() -> None:
    machine = FanucFocasMachine(id="m07", driver="fanuc_focas", ip="192.168.10.57")
    assert machine.signals == ["status", "program", "part_count", "alarms"]
    assert machine.poll_interval_ms == 1000
    for interval in (2000, 5000):
        assert FanucFocasMachine(id="m07", driver="fanuc_focas", ip="x", poll_interval_ms=interval).poll_interval_ms
    for interval in (500, 6000):
        with pytest.raises(ValueError):
            FanucFocasMachine(id="m07", driver="fanuc_focas", ip="x", poll_interval_ms=interval)
    with pytest.raises(ValueError):  # the timeline needs the status
        FanucFocasMachine(id="m07", driver="fanuc_focas", ip="x", signals=["program"])
    with pytest.raises(ValueError):
        FanucFocasMachine(id="m07", driver="fanuc_focas", ip="x", signals=["status", "servo_load"])  # type: ignore[list-item]


def test_focas_driver_puts_the_library_folder_first_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fwlib64.dll loads fwlibe64.dll / fwlib0iD64.dll by bare name: their folder must be first on PATH."""
    from collector.drivers import fanuc_focas

    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(tmp_path), "/bin"]))
    fanuc_focas.prepare_dll_search(tmp_path)
    fanuc_focas.prepare_dll_search(tmp_path)
    assert os.environ["PATH"].split(os.pathsep) == [str(tmp_path), "/usr/bin", "/bin"]
    calls: list[str] = []
    monkeypatch.setattr(fanuc_focas, "WINDOWS", True)
    monkeypatch.setattr(fanuc_focas, "_set_dll_directory", calls.append)
    fanuc_focas.prepare_dll_search(tmp_path / "vendor")
    assert calls == [str(tmp_path / "vendor")]
    assert os.environ["PATH"].split(os.pathsep)[:2] == [str(tmp_path / "vendor"), str(tmp_path)]


def test_buffer_age_is_limited_to_seven_days() -> None:
    assert BufferConfig().max_age_days == 7
    with pytest.raises(ValueError):
        BufferConfig(max_age_days=8)


def test_history_is_replayed_once_even_if_the_buffer_is_lost(tmp_path: Path) -> None:
    from collector.config import CollectorConfig
    from collector.main import history_to_replay

    config = CollectorConfig.model_validate(
        {
            "org": "demo",
            "site": "plant1",
            "collector_id": "edge-sim",
            "mqtt": {"host": "localhost"},
            "buffer": {"path": str(tmp_path / "data" / "buffer.db")},
            "machines": [
                {"id": "sim01", "driver": "simulator", "backfill_hours": 8},
                {"id": "sim02", "driver": "simulator"},  # no history wanted
            ],
        }
    )
    buffer = Buffer(config.buffer.path)
    assert history_to_replay(config, buffer) == {"sim01"}
    buffer.next_seq("sim01")
    assert history_to_replay(config, buffer) == set()  # restart with the same buffer
    buffer.close()

    for leftover in (tmp_path / "data").glob("buffer.db*"):  # the buffer file is lost
        leftover.unlink()
    assert history_to_replay(config, Buffer(config.buffer.path)) == set()


def test_recent_profile_moment_is_kept_across_restarts(tmp_path: Path) -> None:
    from collector.config import CollectorConfig
    from collector.main import recent_profile_since

    config = CollectorConfig.model_validate(
        {
            "org": "demo",
            "site": "plant1",
            "collector_id": "edge-sim",
            "mqtt": {"host": "localhost"},
            "buffer": {"path": str(tmp_path / "data" / "buffer.db")},
            "machines": [
                {"id": "sim01", "driver": "simulator", "recent_profile": "milling_slow", "recent_hours": 2},
                {"id": "sim02", "driver": "simulator"},
            ],
        }
    )
    assert recent_profile_since(config, 10_000.0) == {"sim01": 10_000.0 - 7200}
    assert recent_profile_since(config, 99_999.0) == {"sim01": 10_000.0 - 7200}  # a restart does not move it
