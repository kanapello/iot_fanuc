from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "shared" / "schemas"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def validators() -> dict[str, Draft202012Validator]:
    """Contract validators built from shared/schemas, keyed by schema name."""
    resources: list[tuple[str, Resource[Any]]] = []
    schemas: dict[str, Any] = {}
    for path in SCHEMAS_DIR.glob("*.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schemas[path.stem] = schema
        resource = Resource.from_contents(schema)
        resources += [(schema["$id"], resource), (path.name, resource)]
    registry: Registry[Any] = Registry().with_resources(resources)
    return {name: Draft202012Validator(schema, registry=registry) for name, schema in schemas.items()}
