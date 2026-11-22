"""Log of completed repairs: written when a request is done, or by hand for work done outside the system.

The geometry archive shows the records as events on the trend (ADR 0005); the log itself belongs here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def add_repair(
    session: AsyncSession,
    machine_id: int,
    completed_at: datetime,
    description: str,
    engineer: str,
    user_id: int | None,
    request_id: int | None = None,
) -> int:
    return int(
        (
            await session.execute(
                text(
                    "INSERT INTO repair_records (machine_id, completed_at, description, engineer, created_by, "
                    "request_id) VALUES (:m, :at, :description, :engineer, :user, :request) RETURNING id"
                ),
                {
                    "m": machine_id,
                    "at": completed_at,
                    "description": description.strip(),
                    "engineer": engineer.strip(),
                    "user": user_id,
                    "request": request_id,
                },
            )
        ).scalar_one()
    )


async def list_repairs(session: AsyncSession, machine_id: int) -> list[dict[str, Any]]:
    """Completed repairs of a machine, newest first, voided ones included."""
    rows = await session.execute(
        text(
            "SELECT id, completed_at, description, engineer, created_at, request_id, voided_at, void_reason "
            "FROM repair_records WHERE machine_id = :m ORDER BY completed_at DESC, id DESC"
        ),
        {"m": machine_id},
    )
    return [dict(r._mapping) for r in rows]


async def get_repair(session: AsyncSession, repair_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT id, machine_id, completed_at, request_id, voided_at FROM repair_records WHERE id = :id"),
            {"id": repair_id},
        )
    ).first()
    return dict(row._mapping) if row else None


async def void_repair(session: AsyncSession, repair_id: int, reason: str) -> None:
    """A wrong record is not deleted: it stays in the log with the reason and leaves the trend."""
    await session.execute(
        text("UPDATE repair_records SET voided_at = now(), void_reason = :reason WHERE id = :id"),
        {"id": repair_id, "reason": reason.strip()},
    )
