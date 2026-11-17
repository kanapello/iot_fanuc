"""Current machine state for live screens (Redis key + Pub/Sub channel)."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import journal
from app.admin import service as admin
from app.oee import service as oee
from app.oee.calc import NormTable
from app.timeline import service as timeline


def live_key(machine_id: int) -> str:
    return f"machine:{machine_id}:live"


def live_channel(org_code: str, site_code: str) -> str:
    """One Pub/Sub channel per site."""
    return f"live.{org_code}.{site_code}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def build_live(session: AsyncSession, machine: dict[str, Any], now: datetime) -> dict[str, Any]:
    machine_id = machine["id"]
    state = await timeline.current_state(session, machine_id)
    sample = await timeline.latest_sample(session, machine_id) or {}
    program = sample.get("program") or (state or {}).get("program")
    downtime_since = (state or {}).get("downtime_since")
    reason = await journal.current_reason(session, machine_id, downtime_since, now) if downtime_since else None

    shift_payload = None
    shift = await oee.current_shift(session, machine_id, now)
    if shift is not None:
        norm = NormTable(await admin.cycle_norms(session, machine)).lookup(program, now)
        shift_payload = {
            "name": shift["shift_name"],
            "start": _iso(shift["period_start"]),
            "end": _iso(shift["period_end"]),
            "total_parts": shift["total_parts"],
            "good_parts": shift["good_parts"],
            "scrap_parts": shift["scrap_parts"],
            "plan_now": round(shift["planned_time_s"] / norm) if norm else None,
            "plan_shift": round(shift["planned_total_s"] / norm) if norm else None,
            "availability": shift["availability"],
            "performance": shift["performance"],
            "quality": shift["quality"],
            "oee": shift["oee"],
        }
    return {
        "machine_id": machine_id,
        "code": machine["code"],
        "name": machine["name"],
        "org_id": machine["org_id"],
        "site_id": machine["site_id"],
        "site_name": machine["site_name"],
        "area_id": machine["area_id"],
        "area_name": machine["area_name"],
        "state": (state or {}).get("state", "OFFLINE"),
        "since": _iso((state or {}).get("since")),
        "downtime_since": _iso(downtime_since),
        "reason": reason,
        "setup_active": await timeline.setup_active(session, machine_id),
        "program": program,
        "mode": sample.get("mode"),
        "run": sample.get("run"),
        "feed_actual": sample.get("feed_actual"),
        "spindle_speed": sample.get("spindle_speed"),
        "spindle_load": sample.get("spindle_load"),
        "part_count": sample.get("part_count"),
        "last_ts": _iso(sample.get("ts")),
        "shift": shift_payload,
    }


async def publish_live(redis: Redis, machine: dict[str, Any], payload: dict[str, Any], previous: str | None) -> str:
    """Store the snapshot; notify subscribers of the machine's site only when something changed."""
    encoded = json.dumps(payload, ensure_ascii=False)
    await redis.set(live_key(machine["id"]), encoded)
    if encoded != previous:
        await redis.publish(live_channel(machine["org_code"], machine["site_code"]), encoded)
    return encoded


async def read_live(redis: Redis, machine_ids: Collection[int]) -> dict[int, dict[str, Any]]:
    """Snapshots of ``machine_ids`` (the caller's access scope)."""
    ids = list(machine_ids)
    if not ids:
        return {}
    values = await redis.mget([live_key(m) for m in ids])
    return {m: json.loads(v) for m, v in zip(ids, values, strict=True) if v}
