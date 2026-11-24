#!/usr/bin/env python3
"""FOCAS2 connectivity probe for Fanuc CNC controls (stage 0).

Self-contained on purpose: copy this single file plus the Fanuc FOCAS library
to a shop-floor PC and run

    python focas_probe.py --ip 192.168.10.57 --port 8193 --lib ./vendor/Fwlib64.dll

The probe only reads: it never writes anything into the CNC.

By default (and with ``--watch``, once per second) it reads the minimum: the
system info once (cnc_sysinfo: series, software version, axes), the status
word (decoded), program number and name, the part counter (macro #3901 and
parameter 6711) and the active alarms, with the response time of every call.

``--recon`` reads, once and without repeats, the extended set needed to plan
the collector: feed/spindle/rapid overrides from the PMC (G0012 with inverted
logic, G0030, G0014), the tool number (#4120), timers, servo loads with axis
names, cnc_rddynamic2 in one call, feed, spindle speed and load; optionally
PMC ranges (``--pmc "X0-X15,Y0-Y15"``) and diagnosis numbers (``--diag 300``).
Every signal is read on its own: a failure of one does not stop the others.

``--out probe.txt`` also appends everything to a UTF-8 file.

Fwlib64.dll loads its companions (fwlibe64.dll, fwlib0iD64.dll) by bare name
when it connects. That lookup follows the classic DLL search (PATH), which
``os.add_dll_directory`` does not change: on a live control it ended in
EW_NODLL. So the folder of the library is put first on PATH of the process
(and made the DLL directory with SetDllDirectoryW on Windows) before loading.

Structure layouts follow fwlib32.h for the 0i-D / 30i family. Python bitness
must match the library bitness (Fwlib64.dll -> 64-bit Python,
Fwlib32.dll / libfwlib32.so -> 32-bit Python).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EW_OK = 0
EW_FUNC = 1
EW_LENGTH = 2
EW_ATTRIB = 4
EW_NOOPT = 6
EW_SOCKET = -16
EW_PROTOCOL = -17
EW_HANDLE = -8

# code -> (symbol, human-readable hint shown to the commissioning engineer)
FOCAS_ERRORS: dict[int, tuple[str, str]] = {
    -17: ("EW_PROTOCOL", "ошибка протокола: данные от стойки некорректны (не FOCAS-порт или сбой Ethernet-платы)"),
    -16: (
        "EW_SOCKET",
        "нет связи со стойкой: проверьте IP, порт 8193, кабель, питание ЧПУ, "
        "что FOCAS2/Ethernet включён и порт не закрыт межсетевым экраном",
    ),
    -15: ("EW_NODLL", "не найдена вспомогательная DLL для этой серии ЧПУ (fwlibe*.dll / fwlib0iD*.dll рядом с Fwlib)"),
    -11: ("EW_BUS", "ошибка шины (HSSB)"),
    -10: ("EW_SYSTEM2", "системная ошибка (2) на стороне ЧПУ"),
    -9: ("EW_HSSB", "ошибка связи HSSB"),
    -8: ("EW_HANDLE", "недействительный хендл: соединение потеряно или не было открыто"),
    -7: ("EW_VERSION", "версия библиотеки FOCAS не соответствует версии ПО стойки"),
    -6: ("EW_UNEXP", "непредвиденная ошибка библиотеки"),
    -5: ("EW_SYSTEM", "системная ошибка на стороне ЧПУ"),
    -4: ("EW_PARITY", "ошибка чётности общей памяти"),
    -3: ("EW_MMCSYS", "ошибка драйвера MMC"),
    -2: ("EW_RESET", "стойка в состоянии RESET, функция прервана"),
    -1: ("EW_BUSY", "стойка занята, повторите запрос"),
    1: ("EW_FUNC", "функция не поддерживается этой стойкой (или не выполнен cnc_startupprocess)"),
    2: ("EW_LENGTH", "неверная длина блока данных"),
    3: ("EW_NUMBER", "неверный номер данных (такой переменной/параметра нет)"),
    4: ("EW_ATTRIB", "неверный атрибут данных (тип/ось)"),
    5: ("EW_DATA", "неверные данные"),
    6: ("EW_NOOPT", "нет соответствующей опции ЧПУ"),
    7: ("EW_PROT", "запись запрещена (защита)"),
    8: ("EW_OVRFLOW", "переполнение памяти"),
    9: ("EW_PARAM", "неверная настройка параметров ЧПУ"),
    10: ("EW_BUFFER", "буфер пуст/переполнен"),
    11: ("EW_PATH", "неверный номер канала (path)"),
    12: ("EW_MODE", "неверный режим ЧПУ для этой функции"),
    13: ("EW_REJECT", "запрос отклонён ЧПУ"),
    14: ("EW_DTSRVR", "ошибка Data Server"),
    15: ("EW_ALARM", "функция недоступна из-за активной аварии"),
    16: ("EW_STOP", "ЧПУ остановлено"),
    17: ("EW_PASSWD", "данные защищены паролем"),
}

# cnc_statinfo decoding, 0i-D / 30i family.
AUT_MODES = {
    0: "MDI",
    1: "MEM",
    2: "****",
    3: "EDIT",
    4: "HND",
    5: "JOG",
    6: "T-JOG",
    7: "T-HND",
    8: "INC",
    9: "REF",
    10: "RMT",
}
RUN_STATES = {0: "**** (reset)", 1: "STOP", 2: "HOLD", 3: "STRT", 4: "MSTR"}
MOTION_STATES = {0: "***", 1: "MTN", 2: "DWL"}
MSTB_STATES = {0: "***", 1: "FIN"}
EMERGENCY_STATES = {0: "нет", 1: "EMG (аварийный останов)", 2: "RESET", 3: "WAIT"}
ALARM_STATES = {
    0: "нет",
    1: "ALM (авария)",
    2: "BAT (батарея)",
    3: "FAN (вентилятор)",
    4: "PS warning",
    5: "FSSB warning",
    6: "insulate warning",
    7: "encoder warning",
    8: "PMC alarm",
}
EDIT_STATES = {0: "****", 1: "EDIT", 2: "SEARCH", 3: "OUTPUT", 4: "INPUT", 5: "COMPARE"}
ALARM_TYPES = {
    0: "SW",
    1: "PW",
    2: "IO",
    3: "PS",
    4: "OT",
    5: "OH",
    6: "SV",
    7: "SR",
    8: "MC",
    9: "SP",
    10: "DS",
    11: "IE",
    12: "BG",
    13: "SN",
    15: "EX",
    19: "PC",
}

MAX_ALARMS = 10
MAX_SPINDLES = 4
MAX_AXIS = 32  # fwlib32.h for the 30i / 0i-D family; used when cnc_sysinfo is not available
WINDOWS = os.name == "nt"

# pmc_rdpmcrng address types (FOCAS2) and the limit of one range read by --pmc
PMC_TYPES = {"G": 0, "F": 1, "Y": 2, "X": 3, "A": 4, "R": 5, "T": 6, "K": 7, "C": 8, "D": 9, "M": 10, "N": 11, "E": 12}
MAX_PMC_BYTES = 128
TOOL_MACRO = 4120  # modal T code: the tool number
TIMERS = {0: "время включения", 1: "время работы в автомате", 2: "время резания", 3: "время цикла"}
# G0014 ROV2 ROV1 -> rapid traverse override
RAPID_OVERRIDES = {0: "100 %", 1: "50 %", 2: "25 %", 3: "F0 (параметр 1421)"}


class _Struct(ctypes.Structure):
    # fwlib32.h is compiled with #pragma pack(4) on Windows.
    _pack_ = 4


class IODBTIME(_Struct):
    _fields_ = [("minute", ctypes.c_int32), ("msec", ctypes.c_int32)]


class ODBAXISNAME(_Struct):
    _fields_ = [("name", ctypes.c_char), ("suff", ctypes.c_char)]


def _pmc_struct(count: int) -> type[ctypes.Structure]:
    """IODBPMC for ``count`` bytes (the C union holds at least 20)."""

    class IODBPMC(_Struct):
        _fields_ = [
            ("type_a", ctypes.c_short),
            ("type_d", ctypes.c_short),
            ("datano_s", ctypes.c_ushort),
            ("datano_e", ctypes.c_ushort),
            ("cdata", ctypes.c_ubyte * max(count, 20)),
        ]

    return IODBPMC


def _dynamic2_struct(axes: int) -> type[ctypes.Structure]:
    """ODBDY2 for all axes: the position arrays are ``axes`` long (the maximum controlled axes)."""

    class ODBDY2(_Struct):
        _fields_ = [
            ("dummy", ctypes.c_short),
            ("axis", ctypes.c_short),
            ("alarm", ctypes.c_int32),
            ("prgnum", ctypes.c_int32),
            ("prgmnum", ctypes.c_int32),
            ("seqnum", ctypes.c_int32),
            ("actf", ctypes.c_int32),
            ("acts", ctypes.c_int32),
            ("absolute", ctypes.c_int32 * axes),
            ("machine", ctypes.c_int32 * axes),
            ("relative", ctypes.c_int32 * axes),
            ("distance", ctypes.c_int32 * axes),
        ]

    return ODBDY2


def _diag_struct(size: int) -> type[ctypes.Structure]:
    """ODBDGN with ``size`` bytes of data (one value, or one per axis)."""

    class ODBDGN(_Struct):
        _fields_ = [("datano", ctypes.c_short), ("type", ctypes.c_short), ("data", ctypes.c_ubyte * max(size, 4))]

    return ODBDGN


class ODBSYS(_Struct):
    _fields_ = [
        ("addinfo", ctypes.c_short),
        ("max_axis", ctypes.c_short),
        ("cnc_type", ctypes.c_char * 2),
        ("mt_type", ctypes.c_char * 2),
        ("series", ctypes.c_char * 4),
        ("version", ctypes.c_char * 4),
        ("axes", ctypes.c_char * 2),
    ]


class ODBST(_Struct):
    _fields_ = [
        ("hdck", ctypes.c_short),
        ("tmmode", ctypes.c_short),
        ("aut", ctypes.c_short),
        ("run", ctypes.c_short),
        ("motion", ctypes.c_short),
        ("mstb", ctypes.c_short),
        ("emergency", ctypes.c_short),
        ("alarm", ctypes.c_short),
        ("edit", ctypes.c_short),
    ]


class ODBPRO(_Struct):
    _fields_ = [
        ("dummy", ctypes.c_short * 2),
        ("data", ctypes.c_short),  # running program
        ("mdata", ctypes.c_short),  # main program
    ]


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
    # Only the 2-word (long) member of the union is used; the padding keeps
    # the buffer large enough for any non-axis parameter type.
    _fields_ = [
        ("datano", ctypes.c_short),
        ("type", ctypes.c_short),
        ("ldata", ctypes.c_int32),
        ("_pad", ctypes.c_char * 32),
    ]


class ODBSVLOAD(_Struct):
    _fields_ = [("svload", LOADELM)]


class ODBALMMSG2(_Struct):
    _fields_ = [
        ("alm_no", ctypes.c_int32),
        ("type", ctypes.c_short),
        ("axis", ctypes.c_short),
        ("dummy", ctypes.c_short),
        ("msg_len", ctypes.c_short),
        ("alm_msg", ctypes.c_char * 64),
    ]


class FocasError(Exception):
    """A FOCAS call returned a non-zero EW_* code."""

    def __init__(self, function: str, code: int) -> None:
        self.function = function
        self.code = code
        symbol, hint = FOCAS_ERRORS.get(code, ("EW_?", "неизвестный код ошибки"))
        self.symbol = symbol
        self.hint = hint
        super().__init__(f"{function}: {symbol} ({code}) — {hint}")

    @property
    def unsupported(self) -> bool:
        return self.code in (EW_FUNC, EW_NOOPT)

    @property
    def connection_lost(self) -> bool:
        return self.code in (EW_SOCKET, EW_PROTOCOL, EW_HANDLE)


class ProbeSetupError(Exception):
    """The probe could not even start (library missing, wrong bitness...)."""


def _decode(raw: bytes) -> str:
    text = raw.split(b"\x00", 1)[0]
    for encoding in ("ascii", "cp932", "latin-1"):
        try:
            return text.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return text.decode("latin-1", errors="replace").strip()


def _set_dll_directory(folder: str) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    func = kernel32.SetDllDirectoryW
    func.argtypes = [ctypes.c_wchar_p]
    func.restype = ctypes.c_int
    if not func(folder):
        error = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
        print(f"ПРЕДУПРЕЖДЕНИЕ: SetDllDirectoryW({folder}) не сработал, код {error}", file=sys.stderr)


def prepare_dll_search(folder: Path) -> None:
    """Let Fwlib find its companion libraries (fwlibe64.dll, fwlib0iD64.dll) by bare name.

    They are loaded at connection time through the classic DLL search order, which
    ``os.add_dll_directory`` does not affect. The folder goes first on PATH of this
    process (once), and on Windows it also becomes the DLL directory.
    """
    entry = str(folder)
    same = os.path.normcase(os.path.normpath(entry))
    rest = [
        p for p in os.environ.get("PATH", "").split(os.pathsep) if p and os.path.normcase(os.path.normpath(p)) != same
    ]
    os.environ["PATH"] = os.pathsep.join([entry, *rest])
    if WINDOWS:
        _set_dll_directory(entry)


def load_library(path: str) -> Any:
    lib_path = Path(path).expanduser().resolve()
    if not lib_path.is_file():
        raise ProbeSetupError(
            f"Библиотека FOCAS не найдена: {lib_path}\n"
            "Положите Fwlib64.dll / Fwlib32.dll / libfwlib32.so в edge/vendor/ "
            "(в репозитории не хранится — лицензия Fanuc) и укажите путь через --lib."
        )
    prepare_dll_search(lib_path.parent)
    try:
        if WINDOWS:
            return ctypes.WinDLL(str(lib_path))  # type: ignore[attr-defined,unused-ignore]
        return ctypes.CDLL(str(lib_path))
    except OSError as exc:
        bits = ctypes.sizeof(ctypes.c_void_p) * 8
        raise ProbeSetupError(
            f"Не удалось загрузить {lib_path}: {exc}\n"
            f"Текущий Python — {bits}-битный; разрядность библиотеки должна совпадать "
            "(Fwlib64.dll → 64-битный Python, Fwlib32.dll / libfwlib32.so → 32-битный)."
        ) from exc


@dataclass
class FocasProbe:
    """Thin ctypes wrapper: one handle, one thread (FOCAS is not thread-safe per handle)."""

    lib: Any
    handle: ctypes.c_ushort = field(default_factory=lambda: ctypes.c_ushort(0))
    connected: bool = False
    last_ms: float | None = None  # response time of the last FOCAS call, ms

    def _timed(self, func: Any, *args: Any) -> int:
        started = time.perf_counter()
        try:
            return int(func(*args))
        finally:
            self.last_ms = (time.perf_counter() - started) * 1000

    def startup(self, log_path: str) -> None:
        """Linux libraries require cnc_startupprocess before the first connection."""
        func = getattr(self.lib, "cnc_startupprocess", None)
        if func is None:
            return
        func.restype = ctypes.c_short
        func.argtypes = [ctypes.c_int32, ctypes.c_char_p]
        self._check("cnc_startupprocess", func(0, log_path.encode()))

    def shutdown(self) -> None:
        func = getattr(self.lib, "cnc_exitprocess", None)
        if func is not None:
            func.restype = ctypes.c_short
            func()

    def connect(self, ip: str, port: int, timeout_s: int) -> None:
        func = self.lib.cnc_allclibhndl3
        func.restype = ctypes.c_short
        func.argtypes = [
            ctypes.c_char_p,
            ctypes.c_ushort,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_ushort),
        ]
        code = self._timed(func, ip.encode("ascii"), port, timeout_s, ctypes.byref(self.handle))
        self._check("cnc_allclibhndl3", code)
        self.connected = True

    def disconnect(self) -> None:
        if not self.connected:
            return
        func = self.lib.cnc_freelibhndl
        func.restype = ctypes.c_short
        func.argtypes = [ctypes.c_ushort]
        func(self.handle)
        self.connected = False

    @staticmethod
    def _check(function: str, code: int) -> None:
        if code != EW_OK:
            raise FocasError(function, code)

    def _call(self, name: str, *args: Any) -> None:
        func = getattr(self.lib, name)
        func.restype = ctypes.c_short
        self._check(name, self._timed(func, self.handle, *args))

    def sysinfo(self) -> dict[str, Any]:
        buf = ODBSYS()
        self._call("cnc_sysinfo", ctypes.byref(buf))
        return {
            "addinfo": int(buf.addinfo),
            "max_axis": int(buf.max_axis),
            "cnc_type": _decode(bytes(buf.cnc_type)),
            "mt_type": _decode(bytes(buf.mt_type)),
            "series": _decode(bytes(buf.series)),
            "version": _decode(bytes(buf.version)),
            "axes": _decode(bytes(buf.axes)),
        }

    def statinfo(self) -> dict[str, int]:
        buf = ODBST()
        self._call("cnc_statinfo", ctypes.byref(buf))
        return {name: int(getattr(buf, name)) for name, _ in ODBST._fields_}

    def program_numbers(self) -> tuple[int, int]:
        buf = ODBPRO()
        self._call("cnc_rdprgnum", ctypes.byref(buf))
        return int(buf.data), int(buf.mdata)

    def exe_program_name(self) -> tuple[str, int]:
        buf = ODBEXEPRG()
        self._call("cnc_exeprgname", ctypes.byref(buf))
        return _decode(bytes(buf.name)), int(buf.o_num)

    def actual_feed(self) -> int:
        buf = ODBACT()
        self._call("cnc_actf", ctypes.byref(buf))
        return int(buf.data)

    def actual_spindle_speed(self) -> int:
        buf = ODBACT()
        self._call("cnc_acts", ctypes.byref(buf))
        return int(buf.data)

    def spindle_loads(self) -> list[tuple[str, float]]:
        count = ctypes.c_short(MAX_SPINDLES)
        buf = (ODBSPLOAD * MAX_SPINDLES)()
        self._call("cnc_rdspmeter", ctypes.c_short(0), ctypes.byref(count), buf)
        result = []
        for i in range(max(0, min(int(count.value), MAX_SPINDLES))):
            elem = buf[i].spload
            name = (_decode(bytes(elem.name)) + _decode(bytes(elem.suff1))) or f"S{i + 1}"
            result.append((name, int(elem.data) / (10 ** int(elem.dec))))
        return result

    def macro(self, number: int) -> float | None:
        buf = ODBM()
        self._call("cnc_rdmacro", ctypes.c_short(number), ctypes.c_short(10), ctypes.byref(buf))
        if int(buf.mcr_val) == 0 and int(buf.dec_val) == -1:
            return None  # <vacant> macro variable
        return int(buf.mcr_val) / (10 ** int(buf.dec_val))

    def parameter_long(self, number: int) -> int:
        buf = IODBPSD()
        self._call(
            "cnc_rdparam",
            ctypes.c_short(number),
            ctypes.c_short(0),
            ctypes.c_short(4 + ctypes.sizeof(ctypes.c_int32)),
            ctypes.byref(buf),
        )
        return int(buf.ldata)

    def pmc_bytes(self, letter: str, start: int, end: int) -> bytes:
        """PMC addresses ``start``..``end`` of one type, as bytes (pmc_rdpmcrng, byte data)."""
        count = end - start + 1
        buf = _pmc_struct(count)()
        self._call(
            "pmc_rdpmcrng",
            ctypes.c_short(PMC_TYPES[letter]),
            ctypes.c_short(0),
            ctypes.c_ushort(start),
            ctypes.c_ushort(end),
            ctypes.c_ushort(8 + count),
            ctypes.byref(buf),
        )
        return bytes(buf.cdata[:count])  # type: ignore[attr-defined]

    def timer(self, kind: int) -> tuple[int, int]:
        buf = IODBTIME()
        self._call("cnc_rdtimer", ctypes.c_short(kind), ctypes.byref(buf))
        return int(buf.minute), int(buf.msec)

    def axis_names(self, limit: int) -> list[str]:
        count = ctypes.c_short(limit)
        buf = (ODBAXISNAME * limit)()
        self._call("cnc_rdaxisname", ctypes.byref(count), buf)
        names = []
        for i in range(max(0, min(int(count.value), limit))):
            names.append((_decode(bytes(buf[i].name)) + _decode(bytes(buf[i].suff))) or f"#{i + 1}")
        return names

    def servo_loads(self, limit: int) -> list[tuple[str, float]]:
        count = ctypes.c_short(limit)
        buf = (ODBSVLOAD * limit)()
        self._call("cnc_rdsvmeter", ctypes.byref(count), buf)
        result = []
        for i in range(max(0, min(int(count.value), limit))):
            elem = buf[i].svload
            name = (_decode(bytes(elem.name)) + _decode(bytes(elem.suff1))) or f"#{i + 1}"
            result.append((name, int(elem.data) / (10 ** int(elem.dec))))
        return result

    def dynamic2(self, axes: int) -> dict[str, Any]:
        """cnc_rddynamic2 for all axes in one call."""
        struct = _dynamic2_struct(axes)
        buf: Any = struct()
        self._call("cnc_rddynamic2", ctypes.c_short(-1), ctypes.c_short(ctypes.sizeof(struct)), ctypes.byref(buf))
        return {
            "alarm": int(buf.alarm),
            "prgnum": int(buf.prgnum),
            "prgmnum": int(buf.prgmnum),
            "seqnum": int(buf.seqnum),
            "actf": int(buf.actf),
            "acts": int(buf.acts),
            **{name: [int(v) for v in getattr(buf, name)] for name in ("absolute", "machine", "relative", "distance")},
        }

    def diagnosis(self, number: int, axes: int) -> tuple[str, int, bytes]:
        """One diagnosis number: (kind, bytes per value, raw data).

        The data type is not known in advance: the length is tried as 4, 2 and 1 bytes,
        first as a single value, then (EW_ATTRIB: an axis type) for all axes.
        """
        for kind, axis, count in (("value", 0, 1), ("axes", -1, axes)):
            for size in (4, 2, 1):
                struct = _diag_struct(size * count)
                buf: Any = struct()
                try:
                    self._call(
                        "cnc_diagnoss",
                        ctypes.c_short(number),
                        ctypes.c_short(axis),
                        ctypes.c_short(4 + size * count),
                        ctypes.byref(buf),
                    )
                except FocasError as exc:
                    if exc.code == EW_LENGTH:
                        continue
                    if exc.code == EW_ATTRIB and kind == "value":
                        break
                    raise
                return kind, size, bytes(buf.data[: size * count])
        raise FocasError("cnc_diagnoss", EW_LENGTH)

    def alarms(self) -> list[dict[str, Any]]:
        count = ctypes.c_short(MAX_ALARMS)
        buf = (ODBALMMSG2 * MAX_ALARMS)()
        self._call("cnc_rdalmmsg2", ctypes.c_short(-1), ctypes.byref(count), buf)
        result = []
        for i in range(max(0, min(int(count.value), MAX_ALARMS))):
            item = buf[i]
            length = max(0, min(int(item.msg_len), 64))
            prefix = ALARM_TYPES.get(int(item.type), f"T{int(item.type)}")
            result.append(
                {
                    "code": f"{prefix}{int(item.alm_no):04d}",
                    "axis": int(item.axis),
                    "message": _decode(bytes(item.alm_msg)[:length]),
                }
            )
        return result


def _label(table: dict[int, str], value: int) -> str:
    return f"{table.get(value, '?')} [{value}]"


def format_sysinfo(info: dict[str, Any]) -> list[str]:
    i_series = "i" if info["addinfo"] & 0x2 else ""  # bit 1 of addinfo: an i series control
    model = f"{info['cnc_type']}{i_series}-{info['mt_type']}".strip("-")
    axes = info["axes"].lstrip("0") or info["axes"]
    return [
        f"  модель ЧПУ         : {model} (cnc_type {info['cnc_type']!r}, mt_type {info['mt_type']!r})",
        f"  серия ПО           : {info['series']}",
        f"  версия ПО          : {info['version']}",
        f"  управляемых осей   : {axes} (максимум {info['max_axis']})",
        f"  addinfo            : 0x{info['addinfo'] & 0xFFFF:04x}",
    ]


def _with_time(probe: FocasProbe, line: str) -> str:
    return line if probe.last_ms is None else f"{line}  [{probe.last_ms:.1f} мс]"


def read_system(probe: FocasProbe) -> tuple[list[str], dict[str, Any] | None]:
    """Series, software version and axes: read once after connecting."""
    probe.last_ms = None
    try:
        info = probe.sysinfo()
    except FocasError as exc:
        if exc.connection_lost:
            raise
        note = "не поддерживается стойкой" if exc.unsupported else "ошибка чтения"
        return [_with_time(probe, f"Система (cnc_sysinfo): {note} — {exc}")], None
    return [_with_time(probe, "Система (cnc_sysinfo):"), *format_sysinfo(info)], info


def format_statinfo(stat: dict[str, int]) -> list[str]:
    return [
        f"  режим (aut)        : {_label(AUT_MODES, stat['aut'])}",
        f"  выполнение (run)   : {_label(RUN_STATES, stat['run'])}",
        f"  движение (motion)  : {_label(MOTION_STATES, stat['motion'])}",
        f"  M/S/T/B (mstb)     : {_label(MSTB_STATES, stat['mstb'])}",
        f"  аварийный останов  : {_label(EMERGENCY_STATES, stat['emergency'])}",
        f"  авария (alarm)     : {_label(ALARM_STATES, stat['alarm'])}",
        f"  редактирование     : {_label(EDIT_STATES, stat['edit'])}",
        f"  hdck / tmmode      : {stat['hdck']} / {stat['tmmode']}",
    ]


def _section(probe: FocasProbe, lines: list[str], title: str, reader: Any) -> None:
    """One signal on its own: an error is reported in its line and the next signal is read.

    The first line gets the response time of the FOCAS call. A lost connection stops
    everything: no further call can succeed.
    """
    probe.last_ms = None
    try:
        part = reader()
    except FocasError as exc:
        if exc.connection_lost:
            raise
        note = "не поддерживается стойкой" if exc.unsupported else "ошибка чтения"
        part = [f"{title}: {note} — {exc}"]
    if part:
        part[0] = _with_time(probe, part[0])
    lines.extend(part)


def read_minimum(probe: FocasProbe) -> list[str]:
    """The minimum, read in the normal mode and every second with --watch.

    Status, program number and name, the part counter (both sources) and, only while
    the status shows an alarm, the alarm list.
    """
    lines: list[str] = []
    alarm_active = True  # if statinfo fails, still try to read alarms

    def read_status() -> list[str]:
        nonlocal alarm_active
        stat = probe.statinfo()
        alarm_active = stat["alarm"] != 0 or stat["emergency"] != 0
        return ["Статус (cnc_statinfo):", *format_statinfo(stat)]

    def read_program() -> list[str]:
        running, main = probe.program_numbers()
        return [f"Программа (cnc_rdprgnum): выполняемая O{running:04d}, основная O{main:04d}"]

    def read_program_name() -> list[str]:
        name, o_num = probe.exe_program_name()
        return [f"Имя программы (cnc_exeprgname): {name!r} (O{o_num:04d})"]

    def read_macro() -> list[str]:
        value = probe.macro(3901)
        shown = "<пусто>" if value is None else f"{value:g}"
        return [f"Счётчик деталей, макро #3901 (cnc_rdmacro): {shown}"]

    def read_alarms() -> list[str]:
        if not alarm_active:
            return ["Аварии (cnc_rdalmmsg2): нет активных"]
        alarms = probe.alarms()
        if not alarms:
            return ["Аварии (cnc_rdalmmsg2): нет активных"]
        out = [f"Аварии (cnc_rdalmmsg2): {len(alarms)}"]
        out.extend(f"  {a['code']} (ось {a['axis']}): {a['message']}" for a in alarms)
        return out

    _section(probe, lines, "Статус (cnc_statinfo)", read_status)
    _section(probe, lines, "Программа (cnc_rdprgnum)", read_program)
    _section(probe, lines, "Имя программы (cnc_exeprgname)", read_program_name)
    _section(probe, lines, "Макро #3901 (cnc_rdmacro)", read_macro)
    _section(
        probe,
        lines,
        "Параметр 6711 (cnc_rdparam)",
        lambda: [f"Счётчик деталей, параметр 6711 (cnc_rdparam): {probe.parameter_long(6711)}"],
    )
    _section(probe, lines, "Аварии (cnc_rdalmmsg2)", read_alarms)
    return lines


# ---------------------------------------------------------------- recon


def feed_override_percent(raw: int) -> int:
    """G0012 *FV0..*FV7 are negative logic: a 0 bit adds its weight, so 255 - value (0..254 %)."""
    return 255 - (raw & 0xFF)


def spindle_override_percent(raw: int) -> int:
    """G0030 SOV0..SOV7: the percentage in binary (0..254 %)."""
    return raw & 0xFF


def rapid_override(raw: int) -> str:
    """G0014 ROV1 (bit 0), ROV2 (bit 1)."""
    return RAPID_OVERRIDES[raw & 0x03]


def _byte(raw: int) -> str:
    return f"0x{raw:02X} ({raw:08b})"


def parse_pmc_ranges(text: str) -> list[tuple[str, int, int]]:
    """``"X0-X15,Y0-Y15,R100"`` -> [("X", 0, 15), ("Y", 0, 15), ("R", 100, 100)]."""
    ranges = []
    for item in (part.strip().upper() for part in text.split(",")):
        match = re.fullmatch(r"([A-Z])(\d{1,5})(?:-([A-Z])?(\d{1,5}))?", item)
        if match is None:
            raise ValueError(f"не понимаю диапазон PMC {item!r}: нужно, например, X0-X15")
        letter, first, second_letter, last = match.groups()
        if letter not in PMC_TYPES or (second_letter and second_letter != letter):
            raise ValueError(f"диапазон PMC {item!r}: адреса {', '.join(PMC_TYPES)}, одного типа")
        start, end = int(first), int(last) if last is not None else int(first)
        if end < start or end - start + 1 > MAX_PMC_BYTES:
            raise ValueError(f"диапазон PMC {item!r}: от меньшего к большему, не больше {MAX_PMC_BYTES} байт")
        ranges.append((letter, start, end))
    return ranges


def parse_diag_numbers(text: str) -> list[int]:
    numbers = []
    for item in (part.strip() for part in text.split(",")):
        if not item.isdigit() or not 0 <= int(item) <= 32767:
            raise ValueError(f"номер диагностики {item!r}: нужно целое от 0 до 32767")
        numbers.append(int(item))
    return numbers


def _format_timer(minute: int, msec: int) -> str:
    hours, minutes = divmod(minute, 60)
    shown = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"
    return f"{shown} {msec / 1000:.3f} с (minute={minute}, msec={msec})"


def _format_diag(number: int, kind: str, size: int, raw: bytes, names: list[str]) -> list[str]:
    bits = {1: "байт", 2: "слово", 4: "двойное слово"}[size]
    values = [int.from_bytes(raw[i : i + size], "little", signed=True) for i in range(0, len(raw), size)]
    if kind == "value":
        binary = f", биты {raw[0]:08b}" if size == 1 else ""
        return [f"Диагностика {number} (cnc_diagnoss): {values[0]} ({bits}, 0x{raw.hex().upper()}{binary})"]
    shown = values[: len(names)] if names else values
    pairs = ", ".join(f"{names[i] if i < len(names) else f'#{i + 1}'}={v}" for i, v in enumerate(shown))
    return [f"Диагностика {number} (cnc_diagnoss, по осям, {bits}): {pairs}"]


def read_recon(
    probe: FocasProbe, info: dict[str, Any] | None, pmc: list[tuple[str, int, int]], diags: list[int]
) -> list[str]:
    """The extended set, read once: what the collector could read, and how fast the control answers."""
    lines: list[str] = []
    max_axis = info["max_axis"] if info and 1 <= info["max_axis"] <= MAX_AXIS else MAX_AXIS
    names: list[str] = []

    def override(title: str, address: int, explain: Any) -> None:
        def reader() -> list[str]:
            (raw,) = probe.pmc_bytes("G", address, address)
            return [f"{title} (pmc_rdpmcrng G{address:04d}): {_byte(raw)} → {explain(raw)}"]

        _section(probe, lines, f"{title} (pmc_rdpmcrng G{address:04d})", reader)

    override("Коррекция подачи", 12, lambda raw: f"{feed_override_percent(raw)} % (инверсная логика: 255 − значение)")
    override("Коррекция шпинделя", 30, lambda raw: f"{spindle_override_percent(raw)} %")
    override(
        "Коррекция ускоренного хода",
        14,
        lambda raw: f"ROV2={(raw >> 1) & 1} ROV1={raw & 1} → {rapid_override(raw)}",
    )

    def read_tool() -> list[str]:
        value = probe.macro(TOOL_MACRO)
        shown = "<пусто>" if value is None else f"{value:g}"
        return [f"Инструмент, макро #{TOOL_MACRO} (cnc_rdmacro): {shown}"]

    _section(probe, lines, f"Макро #{TOOL_MACRO} (cnc_rdmacro)", read_tool)
    for kind, label in TIMERS.items():
        _section(
            probe,
            lines,
            f"Таймер «{label}» (cnc_rdtimer {kind})",
            lambda kind=kind, label=label: [
                f"Таймер «{label}» (cnc_rdtimer {kind}): {_format_timer(*probe.timer(kind))}"
            ],
        )

    def read_axis_names() -> list[str]:
        names.extend(probe.axis_names(max_axis))
        return [f"Имена осей (cnc_rdaxisname): {', '.join(names) or 'нет'}"]

    def read_servo() -> list[str]:
        loads = probe.servo_loads(max_axis)
        shown = ", ".join(
            f"{names[i] if i < len(names) else name}={value:g} %" for i, (name, value) in enumerate(loads)
        )
        return [f"Нагрузка сервоосей (cnc_rdsvmeter): {shown or 'нет данных'}"]

    def read_dynamic() -> list[str]:
        d = probe.dynamic2(max_axis)
        count = len(names) or (int(info["axes"]) if info and info["axes"].isdigit() else 0) or max_axis
        out = [
            f"Одним вызовом (cnc_rddynamic2, все оси, буфер на {max_axis}): программа O{d['prgnum']:04d}, "
            f"основная O{d['prgmnum']:04d}, кадр N{d['seqnum']}, подача {d['actf']}, обороты {d['acts']}, "
            f"авария {d['alarm']}"
        ]
        for i in range(min(count, max_axis)):
            axis = names[i] if i < len(names) else f"#{i + 1}"
            out.append(
                f"  {axis}: абсолютная {d['absolute'][i]}, машинная {d['machine'][i]}, "
                f"относительная {d['relative'][i]}, остаток пути {d['distance'][i]}"
            )
        out.append("  координаты — в единицах наименьшего приращения (обычно 0,001 мм)")
        return out

    def read_spindle_load() -> list[str]:
        loads = probe.spindle_loads()
        text = ", ".join(f"{name}={value:g} %" for name, value in loads) or "шпиндели не найдены"
        return [f"Нагрузка шпинделя (cnc_rdspmeter): {text}"]

    _section(probe, lines, "Имена осей (cnc_rdaxisname)", read_axis_names)
    _section(probe, lines, "Нагрузка сервоосей (cnc_rdsvmeter)", read_servo)
    _section(probe, lines, "Одним вызовом (cnc_rddynamic2)", read_dynamic)
    _section(
        probe, lines, "Подача (cnc_actf)", lambda: [f"Фактическая подача (cnc_actf): {probe.actual_feed()} мм/мин"]
    )
    _section(
        probe,
        lines,
        "Обороты (cnc_acts)",
        lambda: [f"Обороты шпинделя (cnc_acts): {probe.actual_spindle_speed()} об/мин"],
    )
    _section(probe, lines, "Нагрузка шпинделя (cnc_rdspmeter)", read_spindle_load)

    for letter, start, end in pmc:

        def read_range(letter: str = letter, start: int = start, end: int = end) -> list[str]:
            data = probe.pmc_bytes(letter, start, end)
            out = [f"PMC {letter}{start:04d}–{letter}{end:04d} (pmc_rdpmcrng):"]
            out.extend(f"  {letter}{start + i:04d} = {_byte(value)}" for i, value in enumerate(data))
            return out

        _section(probe, lines, f"PMC {letter}{start:04d}–{letter}{end:04d} (pmc_rdpmcrng)", read_range)
    for number in diags:
        _section(
            probe,
            lines,
            f"Диагностика {number} (cnc_diagnoss)",
            lambda number=number: _format_diag(number, *probe.diagnosis(number, max_axis), names),
        )
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверка связи со стойкой Fanuc по FOCAS2. Только чтение: в ЧПУ ничего не записывается."
    )
    parser.add_argument("--ip", required=True, help="IP-адрес стойки")
    parser.add_argument("--port", type=int, default=8193, help="TCP-порт FOCAS (по умолчанию 8193)")
    parser.add_argument("--lib", required=True, help="путь к Fwlib64.dll / Fwlib32.dll / libfwlib32.so")
    parser.add_argument("--timeout", type=int, default=10, help="таймаут подключения, с")
    parser.add_argument("--watch", action="store_true", help="повторять минимум раз в секунду (Ctrl+C — выход)")
    parser.add_argument(
        "--recon",
        action="store_true",
        help="один проход расширенного набора: коррекции, инструмент, таймеры, нагрузки осей, cnc_rddynamic2",
    )
    parser.add_argument("--pmc", help='с --recon: диапазоны PMC, например "X0-X15,Y0-Y15"')
    parser.add_argument("--diag", help="с --recon: номера диагностики через запятую, например 300,301")
    parser.add_argument("--interval", type=float, default=1.0, help="период для --watch, с")
    parser.add_argument("--log", default="focas.log", help="файл журнала библиотеки (только Linux)")
    parser.add_argument("--out", help="дописывать весь вывод ещё и в этот файл (UTF-8), параллельно с экраном")
    return parser


class Output:
    """Screen, and with --out also a UTF-8 file: everything the engineer sees ends up in the file."""

    def __init__(self, path: str | None) -> None:
        self._file = Path(path).expanduser().open("a", encoding="utf-8") if path else None

    def line(self, text: str = "", *, error: bool = False) -> None:
        print(text, file=sys.stderr if error else sys.stdout, flush=True)
        if self._file is not None:
            self._file.write(text + "\n")
            self._file.flush()

    def file_only(self, text: str) -> None:
        """A line for the file alone: separates the runs appended to it."""
        if self._file is not None:
            self._file.write(text + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.recon and args.watch:
        parser.error("--recon выполняется один раз и несовместим с --watch")
    if (args.pmc or args.diag) and not args.recon:
        parser.error("--pmc и --diag читаются только вместе с --recon")
    try:
        args.pmc_ranges = parse_pmc_ranges(args.pmc) if args.pmc else []
        args.diag_numbers = parse_diag_numbers(args.diag) if args.diag else []
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = parse_args(argv)
    try:
        out = Output(args.out)
    except OSError as exc:
        print(f"ОШИБКА: не удалось открыть файл вывода {args.out}: {exc}", file=sys.stderr)
        return 2
    try:
        return _run(args, out)
    finally:
        out.close()


def _run(args: argparse.Namespace, out: Output) -> int:
    out.file_only(f"=== focas_probe {time.strftime('%Y-%m-%d %H:%M:%S %z')}: {args.ip}:{args.port}, {args.lib} ===")
    try:
        probe = FocasProbe(load_library(args.lib))
    except ProbeSetupError as exc:
        out.line(f"ОШИБКА: {exc}", error=True)
        return 2

    try:
        probe.startup(args.log)
        out.line(f"Подключение к {args.ip}:{args.port} (таймаут {args.timeout} с)...")
        probe.connect(args.ip, args.port, args.timeout)
        out.line(f"Подключено за {probe.last_ms or 0:.0f} мс.\n")
        system, info = read_system(probe)
        for line in system:
            out.line(line)
        out.line()
        if args.recon:
            out.line("--- Разведка (--recon): один проход, без повторов ---")
            for line in [*read_minimum(probe), *read_recon(probe, info, args.pmc_ranges, args.diag_numbers)]:
                out.line(line)
            return 0
        while True:
            if args.watch:
                out.line(f"--- {time.strftime('%H:%M:%S')} ---")
            for line in read_minimum(probe):
                out.line(line)
            if not args.watch:
                return 0
            out.line()
            time.sleep(args.interval)
    except FocasError as exc:
        took = f" (ответ за {probe.last_ms:.0f} мс)" if probe.last_ms is not None else ""
        out.line(f"ОШИБКА FOCAS: {exc}{took}", error=True)
        return 1
    except KeyboardInterrupt:
        out.line("\nОстановлено.")
        return 0
    finally:
        probe.disconnect()
        probe.shutdown()


if __name__ == "__main__":
    sys.exit(main())
