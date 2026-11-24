"""Every step of a repair request keeps both the moment stated for it and the moment it was entered.

The stated moment (accepted_at, started_at, completed_at; ``ts`` in the history) may be earlier than
the moment of entry when the step is entered later; the customer then sees a mark. Rows written
before this migration have no separate moment of entry: it is taken equal to the stated one.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-24
"""

from __future__ import annotations

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

STEPS = (
    ("accepted_at", "accepted_recorded_at"),
    ("started_at", "started_recorded_at"),
    ("completed_at", "completed_recorded_at"),
)

UPGRADE = (
    *(f"ALTER TABLE repair_requests ADD COLUMN {recorded} timestamptz" for _stated, recorded in STEPS),
    *(f"UPDATE repair_requests SET {recorded} = {stated}" for stated, recorded in STEPS),
    *(
        f"ALTER TABLE repair_requests ADD CONSTRAINT repair_requests_{recorded}_check "
        f"CHECK (({stated} IS NULL) = ({recorded} IS NULL))"
        for stated, recorded in STEPS
    ),
    "ALTER TABLE repair_request_history ADD COLUMN recorded_at timestamptz",
    "UPDATE repair_request_history SET recorded_at = ts",
    "ALTER TABLE repair_request_history ALTER COLUMN recorded_at SET NOT NULL",
    "ALTER TABLE repair_request_history ALTER COLUMN recorded_at SET DEFAULT now()",
)

DOWNGRADE = (
    "ALTER TABLE repair_request_history DROP COLUMN recorded_at",
    *(f"ALTER TABLE repair_requests DROP COLUMN {recorded}" for _stated, recorded in STEPS),
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
