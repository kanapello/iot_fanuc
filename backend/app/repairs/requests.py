"""Repair requests: opened from an alarm with one click or by hand, worked by the service.

Statuses go strictly new -> accepted -> in_progress -> done. Accepting names the executor, a user
with the role "service"; the executor may be changed until the request is done. Completing writes
a record into the repair log that points back to the request.

Reaction and restoration are not stored, they are computed from the machine timeline when read:

* failure — the moment the alarm was raised, or the moment given by hand (by default, when the
  request was opened);
* reaction — from the failure to the start of work (status "in_progress");
* restoration — from the failure to the first moment the machine is in RUN again on the timeline
  (zero if it was running at the moment of the failure).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repairs import records
from app.timeline import service as timeline

STATUSES = ("new", "accepted", "in_progress", "done")
NEXT = {"new": "accepted", "accepted": "in_progress", "in_progress": "done"}
STEP_COLUMN = {"accepted": "accepted_at", "in_progress": "started_at", "done": "completed_at"}

_COLUMNS = (
    "id, machine_id, status, description, alarm_ts, alarm_code, alarm_message, failure_ts, created_by, "
    "created_by_name, created_at, executor_id, executor_name, accepted_at, started_at, completed_at, work_done, "
    "voided_at, void_reason"
)


class RequestConflict(Exception):
    """The request is not in a state that allows the action (HTTP 409)."""


class RequestInputError(ValueError):
    """The action is possible but its data is wrong (HTTP 422)."""


async def create(
    session: AsyncSession,
    machine_id: int,
    description: str,
    failure_ts: datetime,
    alarm: dict[str, Any] | None,
    user_id: int | None,
    user_name: str,
) -> int:
    request_id = int(
        (
            await session.execute(
                text(
                    "INSERT INTO repair_requests (machine_id, description, alarm_ts, alarm_code, alarm_message, "
                    "failure_ts, created_by, created_by_name) VALUES (:m, :description, :alarm_ts, :code, :message, "
                    ":failure, :user, :name) RETURNING id"
                ),
                {
                    "m": machine_id,
                    "description": description.strip(),
                    "alarm_ts": alarm["start_ts"] if alarm else None,
                    "code": alarm["code"] if alarm else None,
                    "message": alarm["message"] if alarm else None,
                    "failure": failure_ts,
                    "user": user_id,
                    "name": user_name,
                },
            )
        ).scalar_one()
    )
    await _history(session, request_id, "created", None, user_id, user_name, None)
    return request_id


async def _history(
    session: AsyncSession,
    request_id: int,
    action: str,
    executor_id: int | None,
    user_id: int | None,
    user_name: str,
    comment: str | None,
    ts: datetime | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO repair_request_history (request_id, ts, action, executor_id, user_id, user_name, comment) "
            "VALUES (:r, COALESCE(:ts, now()), :action, :executor, :user, :name, :comment)"
        ),
        {
            "r": request_id,
            "ts": ts,
            "action": action,
            "executor": executor_id,
            "user": user_id,
            "name": user_name,
            "comment": comment,
        },
    )


async def get(session: AsyncSession, request_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(text(f"SELECT {_COLUMNS} FROM repair_requests WHERE id = :id"), {"id": request_id})
    ).first()
    return dict(row._mapping) if row else None


async def history(session: AsyncSession, request_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            "SELECT ts, action, executor_id, user_name, comment FROM repair_request_history "
            "WHERE request_id = :r ORDER BY ts, id"
        ),
        {"r": request_id},
    )
    return [dict(r._mapping) for r in rows]


async def list_requests(
    session: AsyncSession, machine_ids: Collection[int], open_only: bool = False, limit: int = 500
) -> list[dict[str, Any]]:
    """Requests of the machines, newest first. ``open_only``: not done and not voided."""
    rows = await session.execute(
        text(
            f"SELECT {_COLUMNS} FROM repair_requests WHERE machine_id = ANY(:ids) "
            "AND (NOT :open OR (status <> 'done' AND voided_at IS NULL)) ORDER BY created_at DESC, id DESC LIMIT :n"
        ),
        {"ids": list(machine_ids), "open": open_only, "n": limit},
    )
    return [dict(r._mapping) for r in rows]


AlarmKey = tuple[int, datetime, str | None]  # machine, start of the alarm episode, alarm code


async def for_alarms(session: AsyncSession, keys: Sequence[AlarmKey]) -> dict[AlarmKey, int]:
    """Requests (not voided) opened from the given alarm episodes: (machine, alarm start, code) -> request id."""
    if not keys:
        return {}
    rows = await session.execute(
        text(
            "SELECT r.id, r.machine_id, r.alarm_ts, r.alarm_code FROM repair_requests r "
            "JOIN unnest(CAST(:machines AS bigint[]), CAST(:ts AS timestamptz[]), CAST(:codes AS text[])) "
            "AS k(machine_id, ts, code) ON r.machine_id = k.machine_id AND r.alarm_ts = k.ts "
            "AND r.alarm_code = k.code WHERE r.voided_at IS NULL"
        ),
        {"machines": [k[0] for k in keys], "ts": [k[1] for k in keys], "codes": [k[2] for k in keys]},
    )
    return {(r.machine_id, r.alarm_ts, r.alarm_code): r.id for r in rows}


async def with_times(session: AsyncSession, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add reaction and restoration computed from the timeline (see the module docstring)."""
    restored = await timeline.first_run_at(session, [(r["machine_id"], r["failure_ts"]) for r in requests])
    for request, restored_at in zip(requests, restored, strict=True):
        failure = request["failure_ts"]
        started = request["started_at"]
        request["reaction_s"] = (started - failure).total_seconds() if started else None
        request["restored_at"] = restored_at
        request["restoration_s"] = (restored_at - failure).total_seconds() if restored_at else None
    return requests


def _check_step(request: dict[str, Any], at: datetime, now: datetime) -> None:
    if at > now:
        raise RequestInputError("the moment is in the future")
    previous = max(t for t in (request["created_at"], request["accepted_at"], request["started_at"]) if t is not None)
    if at < previous:
        raise RequestInputError("the moment is earlier than the previous step of the request")


async def advance(
    session: AsyncSession,
    request: dict[str, Any],
    target: str,
    at: datetime,
    now: datetime,
    executor: dict[str, Any] | None,
    work_done: str | None,
    comment: str | None,
    user_id: int | None,
    user_name: str,
) -> None:
    """Move a request one status forward, or change the executor of an accepted one (same status)."""
    if request["voided_at"] is not None:
        raise RequestConflict("the request is voided")
    current = request["status"]
    if target == current and current in ("accepted", "in_progress"):
        if executor is None:
            raise RequestInputError("a new executor is required")
        await session.execute(
            text("UPDATE repair_requests SET executor_id = :e, executor_name = :n WHERE id = :id"),
            {"id": request["id"], "e": executor["id"], "n": executor["name"]},
        )
        await _history(session, request["id"], "reassigned", executor["id"], user_id, user_name, comment)
        return
    if NEXT.get(current) != target:
        raise RequestConflict(f"a request in status '{current}' cannot become '{target}'")
    _check_step(request, at, now)
    if target == "accepted" and executor is None:
        raise RequestInputError("accepting a request names its executor")
    if target == "done" and not (work_done and work_done.strip()):
        raise RequestInputError("describe the work done")
    executor = executor or {"id": request["executor_id"], "name": request["executor_name"]}
    await session.execute(
        text(
            f"UPDATE repair_requests SET status = :status, {STEP_COLUMN[target]} = :at, executor_id = :e, "
            "executor_name = :n, work_done = COALESCE(:work, work_done) WHERE id = :id"
        ),
        {
            "id": request["id"],
            "status": target,
            "at": at,
            "e": executor["id"],
            "n": executor["name"],
            "work": work_done.strip() if work_done else None,
        },
    )
    await _history(session, request["id"], target, executor["id"], user_id, user_name, comment, at)
    if target == "done":
        assert work_done is not None
        await records.add_repair(
            session, request["machine_id"], at, work_done, executor["name"], user_id, request_id=request["id"]
        )


async def void(
    session: AsyncSession, request: dict[str, Any], reason: str, user_id: int | None, user_name: str
) -> None:
    """A request opened by mistake is voided with a reason; a done one is not (void its repair record instead)."""
    if request["voided_at"] is not None:
        raise RequestConflict("the request is already voided")
    if request["status"] == "done":
        raise RequestConflict("a done request cannot be voided")
    await session.execute(
        text("UPDATE repair_requests SET voided_at = now(), void_reason = :reason WHERE id = :id"),
        {"id": request["id"], "reason": reason.strip()},
    )
    await _history(session, request["id"], "voided", None, user_id, user_name, reason.strip())
