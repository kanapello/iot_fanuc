"""QR passport of a machine (ADR 0009): a permanent random code behind the link /m/<code>.

The code is not a token: it only names the machine. Whoever opens the link still signs in and
sees only what their role allows; a wrong code and a machine out of reach look the same.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 8 characters, no look-alikes (0/O, 1/I): the database function new_passport_code() uses this alphabet
CODE = re.compile(r"[2-9A-HJ-NP-Z]{8}")


def normalize(code: str) -> str | None:
    """The code as stored, or None if it cannot be one (then nothing is looked up)."""
    value = code.strip().upper()
    return value if CODE.fullmatch(value) else None


async def machine_by_code(session: AsyncSession, code: str) -> int | None:
    value = normalize(code)
    if value is None:
        return None
    row = await session.execute(text("SELECT id FROM machines WHERE passport_code = :code"), {"code": value})
    return row.scalar_one_or_none()


async def reissue(session: AsyncSession, machine_id: int) -> str:
    """A new code for the machine: the old sticker stops working at once."""
    return str(
        (
            await session.execute(
                text("UPDATE machines SET passport_code = new_passport_code() WHERE id = :id RETURNING passport_code"),
                {"id": machine_id},
            )
        ).scalar_one()
    )


async def sticker_rows(
    session: AsyncSession, machine_ids: Collection[int], active_only: bool = False
) -> list[dict[str, Any]]:
    """What a sticker shows: machine, site, area and the code; in the order of the area."""
    rows = await session.execute(
        text(
            "SELECT m.id, m.code, m.name, m.passport_code, s.name AS site_name, a.name AS area_name "
            "FROM machines m JOIN sites s ON s.id = m.site_id LEFT JOIN areas a ON a.id = m.area_id "
            "WHERE m.id = ANY(:ids) AND (m.is_active OR NOT :active_only) ORDER BY m.sort_order, m.code"
        ),
        {"ids": list(machine_ids), "active_only": active_only},
    )
    return [dict(r._mapping) for r in rows]


def passport_url(base_url: str, code: str) -> str:
    return f"{base_url.rstrip('/')}/m/{code}"
