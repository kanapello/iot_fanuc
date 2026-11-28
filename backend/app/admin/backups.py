"""Results of the backup container (table backup_runs, written by deploy/backup/mm-backup.sh)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def latest(session: AsyncSession) -> dict[str, Any]:
    """The last run of each kind and the last successful backup.

    {"backup": row | None, "restore_check": row | None, "last_good_backup": row | None};
    a row is {"ok", "finished_at", "message"}. No rows at all: backups are not set up here (development).
    """
    result: dict[str, Any] = {"backup": None, "restore_check": None, "last_good_backup": None}
    rows = await session.execute(
        text(
            "SELECT DISTINCT ON (kind) kind, ok, finished_at, message FROM backup_runs "
            "ORDER BY kind, finished_at DESC, id DESC"
        )
    )
    for r in rows:
        result[r.kind] = {"ok": r.ok, "finished_at": r.finished_at, "message": r.message}
    good = (
        await session.execute(
            text(
                "SELECT ok, finished_at, message FROM backup_runs WHERE kind = 'backup' AND ok "
                "ORDER BY finished_at DESC, id DESC LIMIT 1"
            )
        )
    ).first()
    if good is not None:
        result["last_good_backup"] = {"ok": good.ok, "finished_at": good.finished_at, "message": good.message}
    return result
