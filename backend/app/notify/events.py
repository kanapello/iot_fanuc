"""Event types, who gets them by default and the texts of the messages (in Russian, as the users read them)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class EventType:
    roles: frozenset[str]  # who gets it: a role granted on the node of the subject (a machine, a site)
    restores: bool  # its end is announced too ("восстановлено")


EVENT_TYPES: dict[str, EventType] = {
    "alarm": EventType(frozenset({"master", "service"}), True),  # statinfo alarm 1
    "warning": EventType(frozenset({"service"}), True),  # statinfo alarm 2 (battery), 3 (fan)
    "collector_offline": EventType(frozenset({"service", "admin"}), True),
    "buffer_lost": EventType(frozenset({"service", "admin"}), False),
    "stop_no_reason": EventType(frozenset({"master"}), True),
    "repair_new": EventType(frozenset({"service"}), False),
    "repair_status": EventType(frozenset({"admin", "master"}), False),
    "maintenance_due": EventType(frozenset({"service"}), False),
}

WARNINGS = {2: "низкий заряд батареи", 3: "неисправность вентилятора"}
STATUSES = {"accepted": "принята", "in_progress": "в работе", "done": "выполнена", "voided": "аннулирована"}
STATES = {
    "RUN": "работа",
    "IDLE": "ожидание",
    "STOP": "простой",
    "ALARM": "авария",
    "SETUP": "наладка",
    "OFFLINE": "нет связи",
}


def eligible_types(roles: set[str]) -> list[str]:
    """Event types a holder of these roles gets (and can switch off in the profile)."""
    return [name for name, event in EVENT_TYPES.items() if event.roles & roles]


def _link(base_url: str | None, path: str) -> str:
    return f"\n{base_url.rstrip('/')}{path}" if base_url else ""


def machine_title(machine: dict[str, Any]) -> str:
    place = " · ".join(part for part in (machine.get("site_name"), machine.get("area_name")) if part)
    return f"{machine['name']} ({place})" if place else str(machine["name"])


def alarm(machine: dict[str, Any], episode: dict[str, Any] | None, started: datetime, base_url: str | None) -> str:
    detail = ""
    if episode is not None:
        detail = "\n" + " ".join(str(v) for v in (episode.get("code"), episode.get("message")) if v)
    at = quote(started.isoformat(), safe="")
    return f"🔴 Авария — {machine_title(machine)}{detail}" + _link(base_url, f"/alarms?machine={machine['id']}&at={at}")


def warning(machine: dict[str, Any], code: int, base_url: str | None) -> str:
    what = WARNINGS.get(code, f"предупреждение стойки (код {code})")
    return f"⚠️ Предупреждение: {what} — {machine_title(machine)}" + _link(base_url, f"/machines/{machine['id']}")


def collector_offline(collector: dict[str, Any], minutes: int) -> str:
    return f"📡 Коллектор «{collector['name']}» не на связи больше {minutes} мин ({collector_place(collector)})"


def buffer_lost(collector: dict[str, Any]) -> str:
    return (
        f"⚠️ Коллектор «{collector['name']}» ({collector_place(collector)}) запустился с пустым буфером: "
        "данные, не отправленные до этого, потеряны"
    )


def collector_place(collector: dict[str, Any]) -> str:
    return " · ".join(part for part in (collector.get("org_name"), collector.get("site_name")) if part)


def stop_no_reason(machine: dict[str, Any], minutes: int, base_url: str | None) -> str:
    return f"⏸ {machine_title(machine)}: простой {minutes} мин без причины" + _link(
        base_url, f"/operator/{machine['id']}"
    )


def repair_new(request: dict[str, Any], machine: dict[str, Any], base_url: str | None) -> str:
    return f"🛠 Новая заявка №{request['request_id']} — {machine_title(machine)}\n{request['description']}" + _link(
        base_url, f"/repairs?request={request['request_id']}"
    )


def repair_status(request: dict[str, Any], machine: dict[str, Any], base_url: str | None) -> str:
    status = STATUSES.get(request["action"], request["action"])
    executor = f", исполнитель: {request['executor_name']}" if request.get("executor_name") else ""
    return f"🛠 Заявка №{request['request_id']} {status}{executor} — {machine_title(machine)}" + _link(
        base_url, f"/repairs?request={request['request_id']}"
    )


def maintenance_due(plan: dict[str, Any], machine: dict[str, Any], base_url: str | None) -> str:
    remaining = plan["remaining_hours"]
    when = f"осталось {remaining:.0f} ч наработки" if remaining > 0 else f"просрочено на {-remaining:.0f} ч наработки"
    return f"🗓 Подходит ТО «{plan['name']}» — {machine_title(machine)}: {when}" + _link(base_url, "/repairs")


def restored(event_type: str, subject: dict[str, Any], detail: str | None = None) -> str:
    if event_type == "alarm":
        return f"✅ Восстановлено: авария снята — {machine_title(subject)}"
    if event_type == "warning":
        return f"✅ Восстановлено: предупреждение снято — {machine_title(subject)}"
    if event_type == "collector_offline":
        return f"✅ Восстановлено: коллектор «{subject['name']}» снова на связи ({collector_place(subject)})"
    return f"✅ {machine_title(subject)}: простой без причины закончился — {detail}"
