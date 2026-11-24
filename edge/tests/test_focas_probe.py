"""focas_probe.py against a fake Fwlib compiled from fake_fwlib.c."""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

HERE = Path(__file__).parent
PROBE_PATH = HERE.parent / "tools" / "focas_probe.py"


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("focas_probe", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["focas_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


@pytest.fixture(scope="session")
def fake_lib(tmp_path_factory: pytest.TempPathFactory) -> str:
    compiler = shutil.which("gcc") or shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler not available")
    out = tmp_path_factory.mktemp("fwlib") / "libfakefwlib.so"
    subprocess.run(
        [compiler, "-shared", "-fPIC", "-o", str(out), str(HERE / "fake_fwlib.c")],
        check=True,
    )
    return str(out)


EXTENDED = (
    "cnc_actf",
    "cnc_acts",
    "cnc_rdspmeter",
    "pmc_rdpmcrng",
    "cnc_rdtimer",
    "cnc_rdsvmeter",
    "cnc_rdaxisname",
    "cnc_rddynamic2",
    "cnc_diagnoss",
)


@pytest.fixture
def calls(fake_lib: str) -> Callable[[str], int]:
    """How many times the fake library was called, per function (the same library object as the probe's)."""
    lib = ctypes.CDLL(fake_lib)
    lib.fake_calls.restype = ctypes.c_int
    lib.fake_calls.argtypes = [ctypes.c_char_p]
    lib.fake_reset()
    return lambda name: int(lib.fake_calls(name.encode()))


def run(fake_lib: str, *args: str) -> int:
    return int(probe.main(["--lib", fake_lib, "--log", "/dev/null", *args]))


def test_minimum_report_of_a_healthy_machine(
    fake_lib: str, calls: Callable[[str], int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(fake_lib, "--ip", "10.0.0.1") == 0
    out = capsys.readouterr().out
    assert re.search(r"Подключено за \d+ мс", out)
    # system information, once after connecting
    assert "модель ЧПУ         : 0i-M" in out
    assert "серия ПО           : D4F1" in out
    assert "версия ПО          : 16.0" in out
    assert "управляемых осей   : 3 (максимум 32)" in out
    # every call reports its response time
    for call in ("cnc_sysinfo", "cnc_statinfo", "cnc_rdprgnum", "cnc_exeprgname", "cnc_rdmacro", "cnc_rdparam"):
        assert re.search(rf"\({call}\).*\[\d+\.\d мс\]", out), call
    assert "MEM [1]" in out and "STRT [3]" in out
    assert "выполняемая O1234, основная O1000" in out
    assert "'O1234'" in out
    assert "макро #3901 (cnc_rdmacro): 15873" in out
    assert "параметр 6711 (cnc_rdparam): 15873" in out
    assert "нет активных" in out
    # the minimum does not touch anything else: no feed, speed, load, PMC, timers, axes
    assert {name: calls(name) for name in EXTENDED} == dict.fromkeys(EXTENDED, 0)
    assert "мм/мин" not in out and "об/мин" not in out
    assert calls("cnc_rdalmmsg2") == 0  # no alarm in the status: the alarm list is not read
    assert (calls("cnc_sysinfo"), calls("cnc_statinfo")) == (1, 1)


def test_watch_repeats_only_the_minimum(
    fake_lib: str, calls: Callable[[str], int], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pauses = []

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        if len(pauses) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(probe.time, "sleep", sleep)
    assert run(fake_lib, "--ip", "10.0.0.1", "--watch") == 0
    capsys.readouterr()
    assert calls("cnc_statinfo") == 3 and calls("cnc_sysinfo") == 1  # system information only once
    assert pauses == [1.0, 1.0, 1.0]
    assert {name: calls(name) for name in EXTENDED} == dict.fromkeys(EXTENDED, 0)


def test_alarm_and_unsupported_signals(
    fake_lib: str, calls: Callable[[str], int], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(fake_lib, "--ip", "10.0.0.2") == 0, "unsupported signals must not abort the probe"
    out = capsys.readouterr().out
    assert "ALM (авария) [1]" in out
    assert "SV0401 (ось 1): V-READY OFF" in out
    assert "cnc_exeprgname): не поддерживается стойкой" in out and "EW_FUNC (1)" in out
    assert re.search(r"Система \(cnc_sysinfo\): не поддерживается стойкой .*\[\d+\.\d мс\]", out)
    assert calls("cnc_rdalmmsg2") == 1


def test_recon_reads_the_extended_set_once(
    fake_lib: str, calls: Callable[[str], int], capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(fake_lib, "--ip", "10.0.0.1", "--recon", "--pmc", "X0-X3,Y8", "--diag", "1000,300")
    out = capsys.readouterr().out
    assert code == 0
    assert "один проход, без повторов" in out
    # overrides from the PMC: raw byte and percent; the feed override is negative logic
    assert "Коррекция подачи (pmc_rdpmcrng G0012): 0x9B (10011011) → 100 % (инверсная логика" in out
    assert "Коррекция шпинделя (pmc_rdpmcrng G0030): 0x5A (01011010) → 90 %" in out
    assert "Коррекция ускоренного хода (pmc_rdpmcrng G0014): 0x01 (00000001) → ROV2=0 ROV1=1 → 50 %" in out
    assert "Инструмент, макро #4120 (cnc_rdmacro): 12" in out
    assert "Таймер «время включения» (cnc_rdtimer 0): 2057 ч 36 мин 0.000 с" in out
    assert "Таймер «время цикла» (cnc_rdtimer 3): 1 мин 12.345 с" in out
    assert "Имена осей (cnc_rdaxisname): X, Y, Z" in out
    assert "Нагрузка сервоосей (cnc_rdsvmeter): X=12.5 %, Y=8 %, Z=25 %" in out
    assert "cnc_rddynamic2, все оси, буфер на 32): программа O1234, основная O1000, кадр N120" in out
    assert "  Z: абсолютная 30000, машинная 30005, относительная 300, остаток пути 0" in out
    assert "Фактическая подача (cnc_actf): 1250 мм/мин" in out and "S1=37.5 %" in out
    assert "  X0003 = 0x0C (00001100)" in out and "  Y0008 = 0x1A (00011010)" in out
    assert "Диагностика 1000 (cnc_diagnoss): 90 (байт, 0x5A, биты 01011010)" in out
    assert "Диагностика 300 (cnc_diagnoss, по осям, двойное слово): X=-7, Y=-14, Z=-21" in out
    for call in (r"cnc_rdtimer \d", r"pmc_rdpmcrng G0012", r"cnc_rddynamic2, все оси"):
        assert re.search(rf"\({call}.*\[\d+\.\d мс\]", out), call
    # one pass: every extended function once (timers: four kinds, PMC: three overrides and two ranges)
    assert {name: calls(name) for name in EXTENDED if name not in ("pmc_rdpmcrng", "cnc_rdtimer", "cnc_diagnoss")} == {
        "cnc_actf": 1,
        "cnc_acts": 1,
        "cnc_rdspmeter": 1,
        "cnc_rdsvmeter": 1,
        "cnc_rdaxisname": 1,
        "cnc_rddynamic2": 1,
    }
    assert (calls("pmc_rdpmcrng"), calls("cnc_rdtimer"), calls("cnc_statinfo")) == (5, 4, 1)


def test_recon_signals_are_independent(
    fake_lib: str, calls: Callable[[str], int], capsys: pytest.CaptureFixture[str]
) -> None:
    """On the alarm machine several signals fail; every other one is still read."""
    assert run(fake_lib, "--ip", "10.0.0.2", "--recon") == 0
    out = capsys.readouterr().out
    assert "Коррекция ускоренного хода (pmc_rdpmcrng G0014): ошибка чтения — pmc_rdpmcrng: EW_NUMBER (3)" in out
    assert "Таймер «время резания» (cnc_rdtimer 2): не поддерживается стойкой" in out
    assert "Нагрузка сервоосей (cnc_rdsvmeter): не поддерживается стойкой" in out
    assert "Инструмент, макро #4120 (cnc_rdmacro): <пусто>" in out
    # the rest after each failure
    assert "Коррекция шпинделя (pmc_rdpmcrng G0030): 0x5A" in out
    assert "Таймер «время цикла» (cnc_rdtimer 3)" in out
    assert "Одним вызовом (cnc_rddynamic2, все оси, буфер на 32)" in out  # sysinfo unsupported: 32 axes
    assert "Имена осей (cnc_rdaxisname): X, Y, Z" in out
    assert calls("cnc_rddynamic2") == 1


def test_recon_runs_once_and_not_with_watch(fake_lib: str, capsys: pytest.CaptureFixture[str]) -> None:
    for args in (["--recon", "--watch"], ["--pmc", "X0-X1"], ["--diag", "300"]):
        with pytest.raises(SystemExit) as exit_info:
            run(fake_lib, "--ip", "10.0.0.1", *args)
        assert exit_info.value.code == 2, args
    for bad in ("Q0-Q1", "X5-X1", "X0-Y3", "X0-X999", "X"):
        with pytest.raises(SystemExit):
            run(fake_lib, "--ip", "10.0.0.1", "--recon", "--pmc", bad)
    with pytest.raises(SystemExit):
        run(fake_lib, "--ip", "10.0.0.1", "--recon", "--diag", "3a")
    capsys.readouterr()


def test_overrides_are_decoded() -> None:
    # *FV0..*FV7 are negative logic: all bits 1 = 0 %, 0x9B = 100 %, all bits 0 = the maximum
    assert [probe.feed_override_percent(raw) for raw in (0xFF, 0x9B, 0x91, 0x00)] == [0, 100, 110, 255]
    assert [probe.spindle_override_percent(raw) for raw in (0, 90, 100, 120)] == [0, 90, 100, 120]
    assert [probe.rapid_override(raw) for raw in (0b00, 0b01, 0b10, 0b11, 0b1100)] == [
        "100 %",
        "50 %",
        "25 %",
        "F0 (параметр 1421)",
        "100 %",
    ]
    assert probe.parse_pmc_ranges("x0-x15, Y0-15,R100") == [("X", 0, 15), ("Y", 0, 15), ("R", 100, 100)]
    assert probe.parse_diag_numbers("300, 301") == [300, 301]


def test_probe_and_collector_never_write_to_the_cnc() -> None:
    """Only reading functions: no cnc_wr*, pmc_wr*, program download or delete anywhere in the edge code."""
    writer = re.compile(r"\b(cnc_wr\w*|pmc_wr\w*|cnc_download\w*|cnc_dwnstart\w*|cnc_delete\w*|cnc_clr\w*)")
    sources = [PROBE_PATH, *sorted((HERE.parent / "collector").rglob("*.py"))]
    found = {str(path.name): writer.findall(path.read_text(encoding="utf-8")) for path in sources}
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_output_is_also_written_to_a_utf8_file(
    fake_lib: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "probe.txt"
    for ip in ("10.0.0.1", "10.9.9.9"):  # runs are appended, errors included
        probe.main(["--ip", ip, "--lib", fake_lib, "--log", "/dev/null", "--out", str(target)])
    screen = capsys.readouterr()
    written = target.read_text(encoding="utf-8")
    assert written.count("=== focas_probe") == 2
    assert "Счётчик деталей, макро #3901 (cnc_rdmacro): 15873" in written
    assert "ОШИБКА FOCAS: cnc_allclibhndl3: EW_SOCKET (-16)" in written
    for line in screen.out.splitlines() + screen.err.splitlines():
        assert line in written  # everything on the screen is in the file
    unwritable = tmp_path / "missing" / "probe.txt"
    assert probe.main(["--ip", "10.0.0.1", "--lib", fake_lib, "--out", str(unwritable)]) == 2


def test_library_folder_goes_first_on_path(
    fake_lib: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fwlib loads fwlibe64.dll / fwlib0iD64.dll by bare name: only PATH helps (EW_NODLL on a live control)."""
    folder = str(Path(fake_lib).parent)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", folder, "/bin"]))
    assert probe.main(["--ip", "10.0.0.1", "--lib", fake_lib, "--log", "/dev/null"]) == 0
    probe.main(["--ip", "10.0.0.1", "--lib", fake_lib, "--log", "/dev/null"])
    assert os.environ["PATH"].split(os.pathsep) == [folder, "/usr/bin", "/bin"]  # first, and only once

    # on Windows the folder also becomes the DLL directory
    calls: list[str] = []
    monkeypatch.setattr(probe, "WINDOWS", True)
    monkeypatch.setattr(probe, "_set_dll_directory", calls.append)
    probe.prepare_dll_search(tmp_path)
    assert calls == [str(tmp_path)] and os.environ["PATH"].startswith(str(tmp_path) + os.pathsep)
    capsys.readouterr()


def test_unreachable_machine(fake_lib: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = probe.main(["--ip", "10.9.9.9", "--lib", fake_lib, "--log", "/dev/null"])
    err = capsys.readouterr().err
    assert code == 1
    assert "EW_SOCKET (-16)" in err
    assert "нет связи со стойкой" in err


def test_missing_library(capsys: pytest.CaptureFixture[str]) -> None:
    code = probe.main(["--ip", "10.0.0.1", "--lib", "/nonexistent/Fwlib64.dll"])
    err = capsys.readouterr().err
    assert code == 2
    assert "Библиотека FOCAS не найдена" in err


def test_every_error_code_has_a_message() -> None:
    for code in [*range(-17, -14), *range(-11, 0), *range(1, 18)]:
        symbol, hint = probe.FOCAS_ERRORS[code]
        assert symbol.startswith("EW_") and hint
