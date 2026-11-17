"""Demo geometric accuracy data for the simulator stand (only with MM_DEMO_DATA=true).

All values are made up and marked as demo: engineer "Демо-данные", template "Демо".
The circle points are synthesized from the entered parameters to show the polar plot.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import journal
from app.geometry import service as geometry
from app.geometry.schedule import Policy

MILL, LATHE = "ВФ-500", "ТЦ-250"
ENGINEER = "Демо-данные"

MODEL_TOLERANCES: dict[str, dict[str, float]] = {
    MILL: {
        "circularity": 20, "circular_deviation": 25, "circular_hysteresis": 8, "squareness": 15, "backlash": 5,
        "reversal_spike": 6, "lateral_play": 4, "cyclic_error": 3, "servo_mismatch": 1.0, "scaling_error": 30,
        "straightness": 6,
    },
    LATHE: {
        "circularity": 15, "circular_deviation": 20, "circular_hysteresis": 6, "squareness": 12, "backlash": 4,
        "reversal_spike": 5, "lateral_play": 4, "cyclic_error": 3, "servo_mismatch": 0.8, "scaling_error": 25,
        "straightness": 5,
    },
}  # fmt: skip


def _results(plane: str, wear: float) -> list[geometry.ResultIn]:
    """A plausible set of results; ``wear`` from 0 (new) to 1 (worn) moves them towards the limits."""
    a, b = geometry.PLANE_AXES[plane]
    return [
        geometry.ResultIn("circularity", round(8 + 8 * wear, 1)),
        geometry.ResultIn("circularity", round(7 + 7 * wear, 1), direction="CW"),
        geometry.ResultIn("circularity", round(7.5 + 7.5 * wear, 1), direction="CCW"),
        geometry.ResultIn("circular_deviation", round(10 + 9 * wear, 1)),
        geometry.ResultIn("circular_hysteresis", round(2 + 3 * wear, 1)),
        geometry.ResultIn("squareness", round(5 + 12.5 * wear, 1)),
        geometry.ResultIn("backlash", round(1.2 + 2.5 * wear, 1), axis=a),
        geometry.ResultIn("backlash", round(-0.8 - 1.5 * wear, 1), axis=b),
        geometry.ResultIn("reversal_spike", round(1.5 + 2 * wear, 1), axis=a),
        geometry.ResultIn("reversal_spike", round(1.1 + 1.8 * wear, 1), axis=b),
        geometry.ResultIn("lateral_play", round(0.6 + 1.2 * wear, 1), axis=a),
        geometry.ResultIn("lateral_play", round(0.4 + 0.9 * wear, 1), axis=b),
        geometry.ResultIn("cyclic_error", round(0.5 + 0.6 * wear, 1), axis=a),
        geometry.ResultIn("cyclic_error", round(0.4 + 0.5 * wear, 1), axis=b),
        geometry.ResultIn("servo_mismatch", round(0.2 + 0.5 * wear, 2)),
        geometry.ResultIn("scaling_error", round(6 + 14 * wear, 1), axis=a),
        geometry.ResultIn("scaling_error", round(-4 - 10 * wear, 1), axis=b),
        geometry.ResultIn("straightness", round(1.5 + 2.5 * wear, 1), axis=a),
        geometry.ResultIn("straightness", round(1.2 + 2 * wear, 1), axis=b),
    ]


def synthetic_trace(
    radius_mm: float, results: list[geometry.ResultIn], direction: str, arc_deg: int, seed: int
) -> tuple[list[float], list[float]]:
    """Radial deviation (µm) around the circle, built from squareness, backlash, reversal spikes and noise."""
    rng = random.Random(seed)
    value = {(r.parameter, r.axis, r.direction): r.value for r in results}
    axes = [r.axis for r in results if r.parameter == "backlash"]
    squareness = value.get(("squareness", None, None), 0.0)
    backlash = [value.get(("backlash", axis, None), 0.0) for axis in axes]
    spikes = [value.get(("reversal_spike", axis, None), 0.0) for axis in axes]
    sign = 1.0 if direction == "CCW" else -1.0
    start = 0.0 if arc_deg == 360 else -20.0
    angles, deviations = [], []
    for i in range(arc_deg * 4):
        angle = start + i / 4
        t = math.radians(angle)
        dev = squareness * radius_mm / 2000 * math.sin(2 * t)  # an oval along 45°
        # backlash: the axis lags by half its play, towards its direction of travel
        dev += backlash[0] / 2 * math.copysign(1.0, -math.sin(t) * sign) * math.cos(t)
        dev += backlash[1] / 2 * math.copysign(1.0, math.cos(t) * sign) * math.sin(t)
        # reversal spikes where an axis changes direction (0°/180° for the first axis, 90°/270° for the second)
        for reversal, height in ((0, spikes[0]), (180, spikes[0]), (90, spikes[1]), (270, spikes[1])):
            distance = (angle - reversal + 180) % 360 - 180
            dev += height * math.exp(-((distance / 1.5) ** 2))
        dev += rng.gauss(0, 0.15)
        angles.append(round(angle, 2))
        deviations.append(round(dev, 3))
    return angles, deviations


def demo_pdf(title: str) -> bytes:
    """A minimal valid one-page PDF (ASCII text) standing in for a Ballbar 20 report."""
    stream = f"BT /F1 14 Tf 60 780 Td ({title}) Tj 0 -24 Td (Synthetic demo data, not a measurement.) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> "
        "/Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
    out, offsets = "%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("ascii")


async def seed(session: AsyncSession, now: datetime) -> None:
    """Fill the demo organization once; does nothing if it already has ballbar tests."""
    machines: dict[str, dict[str, Any]] = {
        r.code: dict(r._mapping)
        for r in await session.execute(
            text(
                "SELECT m.id, m.code, m.model FROM machines m JOIN sites s ON s.id = m.site_id "
                "JOIN organizations o ON o.id = s.org_id WHERE o.code = 'demo' AND m.code LIKE 'sim0%'"
            )
        )
    }
    if not machines:
        return
    existing = await session.execute(
        text("SELECT 1 FROM ballbar_tests WHERE machine_id = ANY(:ids) LIMIT 1"),
        {"ids": [m["id"] for m in machines.values()]},
    )
    if existing.first() is not None:
        return

    models = {"sim01": MILL, "sim02": MILL, "sim04": MILL, "sim05": MILL, "sim03": LATHE, "sim06": LATHE}
    for code, model in models.items():
        if code in machines:
            await session.execute(
                text("UPDATE machines SET model = COALESCE(model, :model) WHERE id = :id"),
                {"model": model, "id": machines[code]["id"]},
            )
    for model, limits in MODEL_TOLERANCES.items():
        for parameter, limit in limits.items():
            await session.execute(
                text(
                    "INSERT INTO ballbar_tolerances (machine_model, parameter, limit_value, unit) "
                    "VALUES (:model, :parameter, :limit, :unit) ON CONFLICT DO NOTHING"
                ),
                {"model": model, "parameter": parameter, "limit": limit, "unit": geometry.BY_CODE[parameter].unit},
            )

    async def add(code: str, days_ago: float, plane: str, radius: float, feed: float, arc: int, wear: float) -> int:
        results = _results(plane, wear)
        data = geometry.TestIn(
            performed_at=now - timedelta(days=days_ago),
            engineer=ENGINEER,
            plane=plane,
            radius_mm=radius,
            feed_mm_min=feed,
            arc_deg=arc,
            air_temp_c=20.4,
            machine_temp_c=21.8,
            template=f"Демо {plane} R{radius:g} F{feed:g}",
            notes="Демонстрационные данные симулятора, не измерение.",
            results=results,
        )
        return await geometry.create_test(session, machines[code]["id"], data, None)

    if "sim01" in machines:  # a squareness that slowly drifts out of tolerance
        for days, wear in ((330, 0.0), (240, 0.25), (150, 0.55), (45, 1.0)):
            last = await add("sim01", days, "XY", 100, 1000, 360, wear)
        await add("sim01", 46, "ZX", 100, 500, 220, 0.3)
        results = _results("XY", 1.0)
        for number, direction in enumerate(("CW", "CCW")):
            angles, deviations = synthetic_trace(100, results, direction, 360, seed=number)
            await geometry.put_trace(session, last, direction, angles, deviations, None, source="demo")
        await geometry.add_file(session, last, "demo-ballbar-XY.pdf", demo_pdf("Demo ballbar report XY"), None, "demo")
        await geometry.set_policy(session, machines["sim01"]["id"], Policy(interval_days=180, interval_run_hours=4000))
    if "sim02" in machines:  # within tolerance, but a collision was reported after the test
        await add("sim02", 100, "XY", 100, 1000, 360, 0.2)
        await journal.add_collision(
            session, machines["sim02"]["id"], now - timedelta(days=2), "Демо: удар оправки о деталь", "оператор"
        )
    if "sim03" in machines:  # the simulated servo alarms after this test make the next check due
        await add("sim03", 60, "ZX", 100, 500, 220, 0.3)
        await geometry.set_policy(session, machines["sim03"]["id"], Policy(interval_days=120))
    if "sim04" in machines:
        await add("sim04", 400, "XY", 150, 2000, 360, 0.4)  # overdue by date
    if "sim06" in machines:
        await add("sim06", 170, "ZX", 100, 500, 220, 0.2)  # due within two weeks
