"""Collision marks and repair records are voided with a reason, like ballbar tests, instead of being deleted.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

TABLES = ("collision_marks", "repair_records")


def upgrade() -> None:
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN voided_at timestamptz, ADD COLUMN void_reason text, "
            f"ADD CONSTRAINT {table}_void_check CHECK ((voided_at IS NULL) = (void_reason IS NULL))"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT {table}_void_check, DROP COLUMN void_reason, DROP COLUMN voided_at"
        )
