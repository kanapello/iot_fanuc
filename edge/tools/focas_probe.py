#!/usr/bin/env python3
"""FOCAS2 connectivity probe for Fanuc CNC controls (stage 0).

Self-contained on purpose: copy this single file plus the Fanuc FOCAS library
to a shop-floor PC and run

    python focas_probe.py --ip 192.168.10.57 --port 8193 --lib ./vendor/Fwlib64.dll

It connects to the control and prints the status word (decoded), program
number/name, actual feed, spindle speed and load, macro #3901, parameter 6711
and the active alarms. ``--watch`` repeats the readout once per second.

Structure layouts follow fwlib32.h for the 0i-D / 30i family. Python bitness
must match the library bitness (Fwlib64.dll -> 64-bit Python,
Fwlib32.dll / libfwlib32.so -> 32-bit Python).
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EW_OK = 0
EW_FUNC = 1
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


class _Struct(ctypes.Structure):
    # fwlib32.h is compiled with #pragma pack(4) on Windows.
    _pack_ = 4


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
    _fields_ = [("name", ctypes.c_char * 36), ("o_num", ctypes.c_long)]


class ODBACT(_Struct):
    _fields_ = [("dummy", ctypes.c_short * 2), ("data", ctypes.c_long)]


class LOADELM(_Struct):
    _fields_ = [
        ("data", ctypes.c_long),
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
        ("mcr_val", ctypes.c_long),
        ("dec_val", ctypes.c_short),
    ]


class IODBPSD(_Struct):
    # Only the 2-word (long) member of the union is used; the padding keeps
    # the buffer large enough for any non-axis parameter type.
    _fields_ = [
        ("datano", ctypes.c_short),
        ("type", ctypes.c_short),
        ("ldata", ctypes.c_long),
        ("_pad", ctypes.c_char * 32),
    ]


class ODBALMMSG2(_Struct):
    _fields_ = [
        ("alm_no", ctypes.c_long),
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


def load_library(path: str) -> Any:
    lib_path = Path(path).expanduser().resolve()
    if not lib_path.is_file():
        raise ProbeSetupError(
            f"Библиотека FOCAS не найдена: {lib_path}\n"
            "Положите Fwlib64.dll / Fwlib32.dll / libfwlib32.so в edge/vendor/ "
            "(в репозитории не хранится — лицензия Fanuc) и укажите путь через --lib."
        )
    try:
        if os.name == "nt":
            # Fwlib loads its companion DLLs (fwlibe*.dll, fwlib0iD*.dll) from its own directory.
            os.add_dll_directory(str(lib_path.parent))
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

    def startup(self, log_path: str) -> None:
        """Linux libraries require cnc_startupprocess before the first connection."""
        func = getattr(self.lib, "cnc_startupprocess", None)
        if func is None:
            return
        func.restype = ctypes.c_short
        func.argtypes = [ctypes.c_long, ctypes.c_char_p]
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
            ctypes.c_long,
            ctypes.POINTER(ctypes.c_ushort),
        ]
        self._check("cnc_allclibhndl3", func(ip.encode("ascii"), port, timeout_s, ctypes.byref(self.handle)))
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
        self._check(name, func(self.handle, *args))

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
            ctypes.c_short(4 + ctypes.sizeof(ctypes.c_long)),
            ctypes.byref(buf),
        )
        return int(buf.ldata)

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


def read_report(probe: FocasProbe) -> list[str]:
    """Read every signal once. Unsupported/failed signals are reported, not fatal.

    Connection-level errors are re-raised so the caller can stop or reconnect.
    """
    lines: list[str] = []
    alarm_active = True  # if statinfo fails, still try to read alarms

    def section(title: str, reader: Any) -> None:
        try:
            lines.extend(reader())
        except FocasError as exc:
            if exc.connection_lost:
                raise
            note = "сигнал не поддерживается стойкой" if exc.unsupported else "ошибка чтения"
            lines.append(f"{title}: {note} — {exc}")

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

    def read_spindle_load() -> list[str]:
        loads = probe.spindle_loads()
        if not loads:
            return ["Нагрузка шпинделя (cnc_rdspmeter): шпиндели не найдены"]
        text = ", ".join(f"{name}={value:g} %" for name, value in loads)
        return [f"Нагрузка шпинделя (cnc_rdspmeter): {text}"]

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

    section("Статус (cnc_statinfo)", read_status)
    section("Программа (cnc_rdprgnum)", read_program)
    section("Имя программы (cnc_exeprgname)", read_program_name)
    section("Подача (cnc_actf)", lambda: [f"Фактическая подача (cnc_actf): {probe.actual_feed()} мм/мин"])
    section("Обороты (cnc_acts)", lambda: [f"Обороты шпинделя (cnc_acts): {probe.actual_spindle_speed()} об/мин"])
    section("Нагрузка шпинделя (cnc_rdspmeter)", read_spindle_load)
    section("Макро #3901 (cnc_rdmacro)", read_macro)
    section(
        "Параметр 6711 (cnc_rdparam)",
        lambda: [f"Счётчик деталей, параметр 6711 (cnc_rdparam): {probe.parameter_long(6711)}"],
    )
    section("Аварии (cnc_rdalmmsg2)", read_alarms)
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Проверка связи со стойкой Fanuc по FOCAS2 (этап 0).")
    parser.add_argument("--ip", required=True, help="IP-адрес стойки")
    parser.add_argument("--port", type=int, default=8193, help="TCP-порт FOCAS (по умолчанию 8193)")
    parser.add_argument("--lib", required=True, help="путь к Fwlib64.dll / Fwlib32.dll / libfwlib32.so")
    parser.add_argument("--timeout", type=int, default=10, help="таймаут подключения, с")
    parser.add_argument("--watch", action="store_true", help="повторять вывод раз в секунду (Ctrl+C — выход)")
    parser.add_argument("--interval", type=float, default=1.0, help="период для --watch, с")
    parser.add_argument("--log", default="focas.log", help="файл журнала библиотеки (только Linux)")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)

    try:
        probe = FocasProbe(load_library(args.lib))
    except ProbeSetupError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2

    try:
        probe.startup(args.log)
        print(f"Подключение к {args.ip}:{args.port} (таймаут {args.timeout} с)...")
        probe.connect(args.ip, args.port, args.timeout)
        print("Подключено.\n")
        while True:
            if args.watch:
                print(f"--- {time.strftime('%H:%M:%S')} ---")
            print("\n".join(read_report(probe)), flush=True)
            if not args.watch:
                return 0
            print()
            time.sleep(args.interval)
    except FocasError as exc:
        print(f"ОШИБКА FOCAS: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 0
    finally:
        probe.disconnect()
        probe.shutdown()


if __name__ == "__main__":
    sys.exit(main())
