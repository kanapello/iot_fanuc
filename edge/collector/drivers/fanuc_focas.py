"""Fanuc FOCAS2 driver (ctypes over Fwlib), 0i-D / 30i structure layouts.

Derived from tools/focas_probe.py. NOT yet verified on a real control.
One driver instance = one FOCAS handle = one thread.

Fwlib64.dll loads its companions (fwlibe64.dll, fwlib0iD64.dll) by bare name when
it connects, through the classic DLL search (PATH) that ``os.add_dll_directory``
does not change; on a live control that ended in EW_NODLL. The folder of the
library therefore goes first on PATH of the process (and becomes the DLL
directory via SetDllDirectoryW on Windows) before the library is loaded.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from pathlib import Path
from typing import Any

from collector.config import PartCounterConfig
from collector.drivers.base import Alarm, DriverConnectionError, DriverError, MachineSnapshot

log = logging.getLogger(__name__)

EW_OK = 0
EW_UNSUPPORTED = (1, 6)  # EW_FUNC, EW_NOOPT
EW_CONNECTION = (-16, -17, -8)  # EW_SOCKET, EW_PROTOCOL, EW_HANDLE

AUT_MODES = {0: "MDI", 1: "MEM", 3: "EDIT", 4: "HND", 5: "JOG", 6: "T-JOG", 7: "T-HND", 8: "INC", 9: "REF", 10: "RMT"}
RUN_STATES = {0: "RESET", 1: "STOP", 2: "HOLD", 3: "START", 4: "MSTR"}
ALARM_TYPES = {
    0: "SW", 1: "PW", 2: "IO", 3: "PS", 4: "OT", 5: "OH", 6: "SV", 7: "SR",
    8: "MC", 9: "SP", 10: "DS", 11: "IE", 12: "BG", 13: "SN", 15: "EX", 19: "PC",
}  # fmt: skip
MAX_ALARMS = 10
MAX_SPINDLES = 4


class _Struct(ctypes.Structure):
    _pack_ = 4  # fwlib32.h uses #pragma pack(4) on Windows


class ODBST(_Struct):
    _fields_ = [
        (name, ctypes.c_short)
        for name in ("hdck", "tmmode", "aut", "run", "motion", "mstb", "emergency", "alarm", "edit")
    ]


class ODBPRO(_Struct):
    _fields_ = [("dummy", ctypes.c_short * 2), ("data", ctypes.c_short), ("mdata", ctypes.c_short)]


class ODBEXEPRG(_Struct):
    _fields_ = [("name", ctypes.c_char * 36), ("o_num", ctypes.c_int32)]


class ODBACT(_Struct):
    _fields_ = [("dummy", ctypes.c_short * 2), ("data", ctypes.c_int32)]


class LOADELM(_Struct):
    _fields_ = [
        ("data", ctypes.c_int32),
        ("dec", ctypes.c_short),
        ("unit", ctypes.c_short),
        ("name", ctypes.c_char),
        ("suff1", ctypes.c_char),
        ("suff2", ctypes.c_char),
        ("reserve", ctypes.c_char),
    ]


class ODBSPLOAD(_Struct):
    _fields_ = [("spload", LOADELM), ("spspeed", LOADELM)]


class ODBM(_Struct):
    _fields_ = [
        ("datano", ctypes.c_short),
        ("dummy", ctypes.c_short),
        ("mcr_val", ctypes.c_int32),
        ("dec_val", ctypes.c_short),
    ]


class IODBPSD(_Struct):
    _fields_ = [
        ("datano", ctypes.c_short),
        ("type", ctypes.c_short),
        ("ldata", ctypes.c_int32),
        ("_pad", ctypes.c_char * 32),
    ]


class ODBALMMSG2(_Struct):
    _fields_ = [
        ("alm_no", ctypes.c_int32),
        ("type", ctypes.c_short),
        ("axis", ctypes.c_short),
        ("dummy", ctypes.c_short),
        ("msg_len", ctypes.c_short),
        ("alm_msg", ctypes.c_char * 64),
    ]


class FocasError(DriverError):
    def __init__(self, function: str, code: int) -> None:
        self.function = function
        self.code = code
        super().__init__(f"{function} failed with EW code {code}")


_libraries: dict[str, Any] = {}
_library_lock = threading.Lock()
WINDOWS = os.name == "nt"


def _set_dll_directory(folder: str) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    func = kernel32.SetDllDirectoryW
    func.argtypes = [ctypes.c_wchar_p]
    func.restype = ctypes.c_int
    if not func(folder):
        error = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
        log.warning("SetDllDirectoryW(%s) failed with Windows error %d", folder, error)


def prepare_dll_search(folder: Path) -> None:
    """Put the folder of Fwlib first on PATH of the process (once); on Windows make it the DLL directory."""
    entry = str(folder)
    same = os.path.normcase(os.path.normpath(entry))
    rest = [
        p for p in os.environ.get("PATH", "").split(os.pathsep) if p and os.path.normcase(os.path.normpath(p)) != same
    ]
    os.environ["PATH"] = os.pathsep.join([entry, *rest])
    if WINDOWS:
        _set_dll_directory(entry)


def load_library(path: str) -> Any:
    """Load Fwlib once per process (Linux builds also need cnc_startupprocess once)."""
    with _library_lock:
        if path in _libraries:
            return _libraries[path]
        lib_path = Path(path).expanduser().resolve()
        prepare_dll_search(lib_path.parent)
        if WINDOWS:
            lib: Any = ctypes.WinDLL(str(lib_path))  # type: ignore[attr-defined,unused-ignore]
        else:
            lib = ctypes.CDLL(str(lib_path))
        startup = getattr(lib, "cnc_startupprocess", None)
        if startup is not None:
            startup.restype = ctypes.c_short
            startup.argtypes = [ctypes.c_int32, ctypes.c_char_p]
            startup(0, b"focas.log")
        _libraries[path] = lib
        return lib


def _text(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


class FanucFocasDriver:
    def __init__(
        self,
        library_path: str,
        ip: str,
        port: int = 8193,
        part_counter: PartCounterConfig | None = None,
        timeout_s: int = 10,
    ) -> None:
        self._lib = load_library(library_path)
        self._ip = ip
        self._port = port
        self._timeout_s = timeout_s
        self._part_counter = part_counter or PartCounterConfig()
        self._handle = ctypes.c_ushort(0)
        self._connected = False
        self._disabled: set[str] = set()  # signals the control does not support

    def connect(self) -> None:
        func = self._lib.cnc_allclibhndl3
        func.restype = ctypes.c_short
        func.argtypes = [ctypes.c_char_p, ctypes.c_ushort, ctypes.c_int32, ctypes.POINTER(ctypes.c_ushort)]
        code = func(self._ip.encode("ascii"), self._port, self._timeout_s, ctypes.byref(self._handle))
        if code != EW_OK:
            raise DriverConnectionError(f"cnc_allclibhndl3({self._ip}:{self._port}) failed with EW code {code}")
        self._connected = True

    def disconnect(self) -> None:
        if not self._connected:
            return
        func = self._lib.cnc_freelibhndl
        func.restype = ctypes.c_short
        func.argtypes = [ctypes.c_ushort]
        func(self._handle)
        self._connected = False

    def _call(self, name: str, *args: Any) -> None:
        func = getattr(self._lib, name)
        func.restype = ctypes.c_short
        code = func(self._handle, *args)
        if code == EW_OK:
            return
        if code in EW_CONNECTION:
            raise DriverConnectionError(f"{name} failed with EW code {code}")
        raise FocasError(name, code)

    def _optional(self, signal: str, reader: Any) -> Any:
        """Read a signal; disable it for this machine if the control does not support it."""
        if signal in self._disabled:
            return None
        try:
            return reader()
        except FocasError as exc:
            if exc.code in EW_UNSUPPORTED:
                self._disabled.add(signal)
                log.warning("%s: signal '%s' is not supported (EW %d), disabled", self._ip, signal, exc.code)
            else:
                log.warning("%s: reading '%s' failed: %s", self._ip, signal, exc)
            return None

    def _program_name(self) -> str:
        buf = ODBEXEPRG()
        self._call("cnc_exeprgname", ctypes.byref(buf))
        return _text(bytes(buf.name))

    def _actual(self, function: str) -> float:
        buf = ODBACT()
        self._call(function, ctypes.byref(buf))
        return float(buf.data)

    def _spindle_load(self) -> float | None:
        count = ctypes.c_short(MAX_SPINDLES)
        buf = (ODBSPLOAD * MAX_SPINDLES)()
        self._call("cnc_rdspmeter", ctypes.c_short(0), ctypes.byref(count), buf)
        if count.value < 1:
            return None
        elem = buf[0].spload
        return int(elem.data) / float(10 ** int(elem.dec))

    def _part_count(self) -> int | None:
        if self._part_counter.source == "macro":
            macro = ODBM()
            self._call(
                "cnc_rdmacro", ctypes.c_short(self._part_counter.address), ctypes.c_short(10), ctypes.byref(macro)
            )
            if int(macro.mcr_val) == 0 and int(macro.dec_val) == -1:
                return None  # vacant variable
            return round(int(macro.mcr_val) / float(10 ** int(macro.dec_val)))
        param = IODBPSD()
        self._call(
            "cnc_rdparam",
            ctypes.c_short(self._part_counter.address),
            ctypes.c_short(0),
            ctypes.c_short(8),
            ctypes.byref(param),
        )
        return int(param.ldata)

    def read_snapshot(self) -> MachineSnapshot:
        stat = ODBST()
        self._call("cnc_statinfo", ctypes.byref(stat))
        program = ODBPRO()
        self._call("cnc_rdprgnum", ctypes.byref(program))
        aut, run, alarm, emergency = int(stat.aut), int(stat.run), int(stat.alarm), int(stat.emergency)
        program_number = f"O{int(program.data):04d}"
        return MachineSnapshot(
            mode=AUT_MODES.get(aut, f"AUT{aut}"),
            aut_code=aut,
            run=RUN_STATES.get(run),
            run_code=run,
            motion=int(stat.motion) != 0,
            alarm_code=alarm,
            alarm=alarm == 1,
            emergency_code=emergency,
            emergency=emergency == 1,
            program=program_number,
            program_name=self._optional("program_name", self._program_name) or program_number,
            feed_actual=self._optional("feed_actual", lambda: self._actual("cnc_actf")),
            spindle_speed=self._optional("spindle_speed", lambda: self._actual("cnc_acts")),
            spindle_load=self._optional("spindle_load", self._spindle_load),
            part_count=self._optional("part_count", self._part_count),
        )

    def read_alarms(self) -> list[Alarm]:
        if "alarms" in self._disabled:
            return []
        count = ctypes.c_short(MAX_ALARMS)
        buf = (ODBALMMSG2 * MAX_ALARMS)()
        try:
            self._call("cnc_rdalmmsg2", ctypes.c_short(-1), ctypes.byref(count), buf)
        except FocasError as exc:
            if exc.code in EW_UNSUPPORTED:
                self._disabled.add("alarms")
            log.warning("%s: reading alarms failed: %s", self._ip, exc)
            return []
        alarms = []
        for i in range(max(0, min(int(count.value), MAX_ALARMS))):
            item = buf[i]
            prefix = ALARM_TYPES.get(int(item.type), f"T{int(item.type)}")
            length = max(0, min(int(item.msg_len), 64))
            alarms.append(
                Alarm(
                    code=f"{prefix}{int(item.alm_no):04d}", message=_text(bytes(item.alm_msg)[:length]), category=prefix
                )
            )
        return alarms
