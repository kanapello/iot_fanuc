"""Demo maintenance plans for the simulator stand (only with MM_DEMO_DATA=true).

Short intervals on purpose: the stand has hours of history, not months, so some plans are due
or nearly due right away and the reminders on the service screen are not empty.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.repairs import maintenance

NOTES = "Демонстрационный регламент симулятора."
PLANS = (
    ("sim01", "Смазка направляющих", 4.0),
    ("sim01", "Замена масла в шпиндельном узле", 500.0),
    ("sim02", "Очистка бака СОЖ", 6.0),
    ("sim03", "Проверка давления гидростанции", 200.0),
    ("sim04", "Смазка направляющих", 5.0),
)


async def seed(session: AsyncSession, now: datetime) -> None:
    """Add the plans once; does nothing if the demo machines already have any."""
    machines: dict[str, int] = {
        r.code: r.id
        for r in await session.execute(
            text(
                "SELECT m.id, m.code FROM machines m JOIN sites s ON s.id = m.site_id "
                "JOIN organizations o ON o.id = s.org_id WHERE o.code = 'demo' AND m.code LIKE 'sim0%'"
            )
        )
    }
    if not machines:
        return
    existing = await session.execute(
        text("SELECT 1 FROM maintenance_plans WHERE machine_id = ANY(:ids) LIMIT 1"), {"ids": list(machines.values())}
    )
    if existing.first() is not None:
        return
    for code, name, interval in PLANS:
        if code in machines:
            await maintenance.create_plan(session, machines[code], name, interval, now - timedelta(days=1), NOTES, None)
