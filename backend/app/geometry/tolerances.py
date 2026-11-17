"""Which tolerance applies to a result, and whether the result exceeds it (pure functions)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tolerance:
    id: int
    limit: float
    unit: str
    parameter: str
    machine_id: int | None = None  # a tolerance of one machine ...
    machine_model: str | None = None  # ... or of every machine of a model
    plane: str | None = None  # None = any plane
    axis: str | None = None  # None = any axis

    @property
    def source(self) -> str:
        return "machine" if self.machine_id is not None else "model"


def pick(
    tolerances: Iterable[Tolerance],
    machine_id: int,
    machine_model: str | None,
    plane: str,
    parameter: str,
    axis: str | None,
) -> Tolerance | None:
    """The most specific tolerance: machine before model, then a stated plane, then a stated axis."""
    best: tuple[tuple[int, int, int], Tolerance] | None = None
    for tol in tolerances:
        if tol.parameter != parameter:
            continue
        if tol.machine_id is not None and tol.machine_id != machine_id:
            continue
        if tol.machine_id is None and (machine_model is None or tol.machine_model != machine_model):
            continue
        if tol.plane is not None and tol.plane != plane:
            continue
        if tol.axis is not None and tol.axis != axis:
            continue
        rank = (int(tol.machine_id is not None), int(tol.plane is not None), int(tol.axis is not None))
        if best is None or rank > best[0]:
            best = (rank, tol)
    return None if best is None else best[1]


def exceeds(value: float, tolerance: Tolerance | None) -> bool:
    """Ballbar errors are signed (direction of the error); the tolerance limits their magnitude."""
    return tolerance is not None and abs(value) > tolerance.limit
