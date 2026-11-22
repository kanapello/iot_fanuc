"""Maintenance by running hours: plans "every N hours of RUN" and records of the work done.

Running hours are counted on the timeline from the last record of a plan (or from the moment the
plan starts counting) up to now; they are computed when read and never stored. A plan is "due" once
the hours reach the interval and "soon" from 90 % of it. The forecast date assumes the machine keeps
running as many hours a day as it did over the last 30 days (or since its data begins).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.timeline import service as timeline

SOON_SHARE = 0.9
RATE_WINDOW = timedelta(days=30)
MIN_RATE_WINDOW = timedelta(hours=1)
MAX_FORECAST_DAYS = 3650


async def create_plan(
    session: AsyncSession,
    machine_id: int,
    name: str,
    interval_run_hours: float,
    counted_from: datetime,
    notes: str | None,
    user_id: int | None,
) -> int:
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO maintenance_plans (machine_id, name, interval_run_hours, counted_from, notes, "
                    "created_by) VALUES (:m, :name, :interval, :from_, :notes, :user) RETURNING id"
                ),
                {
                    "m": machine_id,
                    "name": name.strip(),
                    "interval": interval_run_hours,
                    "from_": counted_from,
                    "notes": notes,
                    "user": user_id,
                },
            )
        ).scalar_one()
    )


async def update_plan(
    session: AsyncSession, plan_id: int, name: str, interval_run_hours: float, notes: str | None, is_active: bool
) -> None:
    await session.execute(
        text(
            "UPDATE maintenance_plans SET name = :name, interval_run_hours = :interval, notes = :notes, "
            "is_active = :active WHERE id = :id"
        ),
        {"id": plan_id, "name": name.strip(), "interval": interval_run_hours, "notes": notes, "active": is_active},
    )


async def get_plan(session: AsyncSession, plan_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT id, machine_id, name, interval_run_hours, counted_from, notes, is_active "
                "FROM maintenance_plans WHERE id = :id"
            ),
            {"id": plan_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def add_record(
    session: AsyncSession, plan_id: int, performed_at: datetime, engineer: str, comment: str | None, user_id: int | None
) -> int:
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO maintenance_records (plan_id, performed_at, engineer, comment, created_by) "
                    "VALUES (:plan, :at, :engineer, :comment, :user) RETURNING id"
                ),
                {
                    "plan": plan_id,
                    "at": performed_at,
                    "engineer": engineer.strip(),
                    "comment": comment,
                    "user": user_id,
                },
            )
        ).scalar_one()
    )


async def list_records(session: AsyncSession, plan_id: int) -> list[dict[str, Any]]:
    """Work done under a plan, newest first, voided records included."""
    rows = await session.execute(
        text(
            "SELECT id, performed_at, engineer, comment, created_at, voided_at, void_reason FROM maintenance_records "
            "WHERE plan_id = :plan ORDER BY performed_at DESC, id DESC"
        ),
        {"plan": plan_id},
    )
    return [dict(r._mapping) for r in rows]


async def get_record(session: AsyncSession, record_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT r.id, r.plan_id, p.machine_id, r.voided_at FROM maintenance_records r "
                "JOIN maintenance_plans p ON p.id = r.plan_id WHERE r.id = :id"
            ),
            {"id": record_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def void_record(session: AsyncSession, record_id: int, reason: str) -> None:
    """A record entered by mistake is voided with a reason: the plan counts again from the previous one."""
    await session.execute(
        text("UPDATE maintenance_records SET voided_at = now(), void_reason = :reason WHERE id = :id"),
        {"id": record_id, "reason": reason.strip()},
    )


async def _run_rate(session: AsyncSession, machine_ids: Collection[int], now: datetime) -> dict[int, float]:
    """RUN hours per day over the last 30 days, or since the timeline of the machine begins."""
    first = await timeline.first_interval_at(session, machine_ids)
    spans = []
    for machine_id in machine_ids:
        begin = max(now - RATE_WINDOW, first.get(machine_id) or now)
        spans.append((machine_id, begin, now))
    seconds = await timeline.run_seconds(session, spans)
    rates = {}
    for (machine_id, begin, _end), run in zip(spans, seconds, strict=True):
        window = now - begin
        rates[machine_id] = run / 3600 / (window.total_seconds() / 86400) if window >= MIN_RATE_WINDOW else 0.0
    return rates


async def schedule(
    session: AsyncSession, machine_ids: Collection[int], now: datetime, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Plans of the machines with running hours since the last work, what remains and the forecast date."""
    rows = await session.execute(
        text(
            "SELECT p.id, p.machine_id, p.name, p.interval_run_hours, p.counted_from, p.notes, p.is_active, "
            "(SELECT max(r.performed_at) FROM maintenance_records r WHERE r.plan_id = p.id AND r.voided_at IS NULL "
            "AND r.performed_at <= :now) AS last_done_at FROM maintenance_plans p "
            "WHERE p.machine_id = ANY(:ids) AND (p.is_active OR :all) ORDER BY p.machine_id, p.name, p.id"
        ),
        {"ids": list(machine_ids), "now": now, "all": include_inactive},
    )
    plans = [dict(r._mapping) for r in rows]
    for plan in plans:
        plan["since"] = min(now, max(plan["counted_from"], plan["last_done_at"] or plan["counted_from"]))
    run = await timeline.run_seconds(session, [(p["machine_id"], p["since"], now) for p in plans])
    rates = await _run_rate(session, {p["machine_id"] for p in plans}, now)
    for plan, seconds in zip(plans, run, strict=True):
        hours = seconds / 3600
        interval = plan["interval_run_hours"]
        remaining = interval - hours
        rate = rates.get(plan["machine_id"], 0.0)
        plan["run_hours"] = round(hours, 2)
        plan["remaining_hours"] = round(remaining, 2)
        plan["used_share"] = round(hours / interval, 3)
        plan["status"] = "due" if remaining <= 0 else "soon" if hours >= SOON_SHARE * interval else "ok"
        plan["run_hours_per_day"] = round(rate, 2)
        days = remaining / rate if remaining > 0 and rate > 0 else None
        plan["forecast_at"] = now + timedelta(days=days) if days is not None and days <= MAX_FORECAST_DAYS else None
    return plans
