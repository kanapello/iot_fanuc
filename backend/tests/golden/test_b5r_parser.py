"""Golden tests of the .b5r parser: sample file -> the numbers of its PDF report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.geometry.b5r import parse_b5r

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ballbar"
SAMPLES = sorted(FIXTURES.glob("*.b5r"))


@pytest.mark.skipif(not SAMPLES, reason="no .b5r samples in tests/fixtures/ballbar yet")
@pytest.mark.parametrize("sample", SAMPLES, ids=[s.stem for s in SAMPLES])
def test_b5r_matches_its_report(sample: Path) -> None:
    expected = json.loads(sample.with_suffix(".expected.json").read_text(encoding="utf-8"))
    assert sample.with_suffix(".pdf").exists(), "keep the PDF report next to the sample"
    parsed = parse_b5r(sample.read_bytes())
    setup = expected["setup"]
    assert (parsed.plane, parsed.radius_mm, parsed.feed_mm_min, parsed.arc_deg) == (
        setup["plane"], setup["radius_mm"], setup["feed_mm_min"], setup["arc_deg"],
    )  # fmt: skip
    got = {(r.parameter, r.axis, r.direction): r.value for r in parsed.results}
    for item in expected["results"]:
        key = (item["parameter"], item["axis"], item["direction"])
        assert key in got, f"{key} is missing"
        assert got[key] == pytest.approx(item["value"], abs=expected.get("abs_tolerance", 0.05)), key
