"""Ballbar test parameters (Renishaw QC20, Ballbar 20) and their units.

Names for people live in the UI dictionary; here are codes, units and on which
axes / run directions a parameter is reported. A per-axis parameter is reported
for both axes of the test plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Plane = Literal["XY", "YZ", "ZX"]
PLANES: tuple[Plane, ...] = ("XY", "YZ", "ZX")
PLANE_AXES: dict[str, tuple[str, str]] = {"XY": ("X", "Y"), "YZ": ("Y", "Z"), "ZX": ("Z", "X")}
ARCS = (360, 220)
DIRECTIONS = ("CW", "CCW")

UM, UM_PER_M, PPM, MS = "µm", "µm/m", "ppm", "ms"


@dataclass(frozen=True, slots=True)
class Parameter:
    code: str
    name: str  # English, for API consumers; the UI has its own dictionary
    unit: str
    group: Literal["iso230_4", "diagnostics"]
    per_axis: bool = False
    # allowed run directions; None = both runs together (bidirectional)
    directions: tuple[str | None, ...] = (None,)

    def keys(self, plane: str) -> list[tuple[str | None, str | None]]:
        """Every (axis, direction) a value of this parameter may have in a test of ``plane``."""
        axes: tuple[str | None, ...] = PLANE_AXES[plane] if self.per_axis else (None,)
        return [(axis, direction) for axis in axes for direction in self.directions]


CATALOG: tuple[Parameter, ...] = (
    # ISO 230-4
    Parameter("circularity", "Circularity", UM, "iso230_4", directions=(None, "CW", "CCW")),
    Parameter("circular_deviation", "Circular deviation G", UM, "iso230_4", directions=(None, "CW", "CCW")),
    Parameter("circular_hysteresis", "Circular hysteresis H", UM, "iso230_4"),
    # Renishaw diagnostics
    Parameter("squareness", "Squareness", UM_PER_M, "diagnostics"),
    Parameter("backlash", "Backlash", UM, "diagnostics", per_axis=True),
    Parameter("reversal_spike", "Reversal spike", UM, "diagnostics", per_axis=True),
    Parameter("lateral_play", "Lateral play", UM, "diagnostics", per_axis=True),
    Parameter("cyclic_error", "Cyclic error", UM, "diagnostics", per_axis=True),
    Parameter("servo_mismatch", "Servo mismatch", MS, "diagnostics"),
    Parameter("scaling_error", "Scaling error", PPM, "diagnostics", per_axis=True),
    Parameter("straightness", "Straightness", UM, "diagnostics", per_axis=True),
)
BY_CODE = {p.code: p for p in CATALOG}


class InvalidResultError(ValueError):
    pass


def check_result(plane: str, parameter: str, axis: str | None, direction: str | None) -> Parameter:
    """The catalog entry, if (parameter, axis, direction) is a valid key for a test of ``plane``."""
    entry = BY_CODE.get(parameter)
    if entry is None:
        raise InvalidResultError(f"unknown parameter {parameter!r}")
    if (axis, direction) not in entry.keys(plane):
        raise InvalidResultError(f"{parameter}: axis {axis!r} / direction {direction!r} not valid for plane {plane}")
    return entry
