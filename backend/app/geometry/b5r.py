"""Automatic analysis of Renishaw Ballbar 20 result files (.b5r) — not implemented yet.

The format is not public. The parser is written against real samples: every
``backend/tests/fixtures/ballbar/<name>.b5r`` comes with the PDF report of the
same test and ``<name>.expected.json`` with the numbers of that report (see the
README there). Until then results are entered by hand and the originals are kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.geometry.service import ResultIn


@dataclass(slots=True)
class ParsedTrace:
    direction: str  # CW | CCW
    angle_deg: list[float]
    deviation_um: list[float]
    sample_rate_hz: float | None = None


@dataclass(slots=True)
class ParsedBallbar:
    plane: str
    radius_mm: float
    feed_mm_min: float
    arc_deg: int
    results: list[ResultIn] = field(default_factory=list)
    traces: list[ParsedTrace] = field(default_factory=list)


def parse_b5r(content: bytes) -> ParsedBallbar:
    raise NotImplementedError("the .b5r parser is written once sample files are in backend/tests/fixtures/ballbar/")
