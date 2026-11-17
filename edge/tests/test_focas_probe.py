"""focas_probe.py against a fake Fwlib compiled from fake_fwlib.c."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
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


def test_healthy_machine_report(fake_lib: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = probe.main(["--ip", "10.0.0.1", "--lib", fake_lib, "--log", "/dev/null"])
    out = capsys.readouterr().out
    assert code == 0
    assert "MEM [1]" in out
    assert "STRT [3]" in out
    assert "выполняемая O1234, основная O1000" in out
    assert "'O1234'" in out
    assert "1250 мм/мин" in out
    assert "8000 об/мин" in out
    assert "S1=37.5 %" in out
    assert "макро #3901 (cnc_rdmacro): 15873" in out
    assert "параметр 6711 (cnc_rdparam): 15873" in out
    assert "нет активных" in out


def test_alarm_and_unsupported_signals(fake_lib: str, capsys: pytest.CaptureFixture[str]) -> None:
    code = probe.main(["--ip", "10.0.0.2", "--lib", fake_lib, "--log", "/dev/null"])
    out = capsys.readouterr().out
    assert code == 0, "unsupported signals must not abort the probe"
    assert "ALM (авария) [1]" in out
    assert "SV0401 (ось 1): V-READY OFF" in out
    assert "cnc_exeprgname): сигнал не поддерживается стойкой" in out
    assert "EW_FUNC (1)" in out
    assert "cnc_rdspmeter): сигнал не поддерживается стойкой" in out


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
