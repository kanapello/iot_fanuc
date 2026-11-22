"""Rules of the geometry archive: parameter keys per plane and which tests are comparable."""

from __future__ import annotations

import pytest

from app.geometry.catalog import BY_CODE, InvalidResultError, check_result
from app.geometry.service import group_key


def test_parameter_keys_follow_the_plane() -> None:
    assert BY_CODE["backlash"].keys("XY") == [("X", None), ("Y", None)]
    assert BY_CODE["backlash"].keys("ZX") == [("Z", None), ("X", None)]
    assert BY_CODE["circularity"].keys("YZ") == [(None, None), (None, "CW"), (None, "CCW")]
    assert BY_CODE["squareness"].keys("XY") == [(None, None)]
    assert check_result("XY", "scaling_error", "Y", None).unit == "ppm"
    assert check_result("XY", "servo_mismatch", None, None).unit == "ms"
    assert check_result("ZX", "squareness", None, None).unit == "µm/m"
    for wrong in [("XY", "backlash", "Z", None), ("XY", "squareness", "X", None), ("XY", "backlash", "X", "CW"),
                  ("XY", "circular_hysteresis", None, "CW"), ("XY", "flatness", None, None)]:  # fmt: skip
        with pytest.raises(InvalidResultError):
            check_result(*wrong)


def test_tests_are_compared_by_template_or_by_the_full_set_up() -> None:
    # with a template the template decides, ignoring case and spaces around it
    assert group_key("XY 100 F1000", "XY", 100, 1000, 360) == group_key("  xy 100 f1000 ", "XY", 150, 2000, 220)
    # without a template: plane, radius, feed and arc must all match
    setup = group_key(None, "XY", 100, 1000, 360)
    assert setup == group_key("", "XY", 100.0, 1000.0, 360) == group_key("   ", "XY", 100, 1000, 360)
    assert setup != group_key(None, "XY", 100, 1000, 220)  # the arc is part of the key
    assert setup != group_key(None, "ZX", 100, 1000, 360)
    assert setup != group_key(None, "XY", 150, 1000, 360)
    assert setup != group_key(None, "XY", 100, 2000, 360)
    assert setup != group_key("XY 100 F1000", "XY", 100, 1000, 360)  # a template group is a group of its own
