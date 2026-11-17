"""Core hardening: persistent OEE recalculation marks, later compression, resolved annotations.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # "Recompute OEE from this moment" survives worker restarts (same idea as timeline_dirty).
    op.execute("CREATE TABLE oee_dirty (machine_id text PRIMARY KEY, from_ts timestamptz NOT NULL)")

    # Late data (edge buffer, at most 7 days old) must never land in a compressed chunk.
    op.execute("SELECT remove_compression_policy('telemetry')")
    op.execute("SELECT add_compression_policy('telemetry', INTERVAL '30 days')")

    # An open downtime reason lasts until the machine runs again. The end is derived
    # on read, so nobody but the operator ever modifies downtime_annotations.
    op.execute(
        """
        CREATE VIEW downtime_annotations_resolved AS
        SELECT a.*,
               COALESCE(a.end_ts, (
                   SELECT min(i.start_ts) FROM state_intervals i
                   WHERE i.machine_id = a.machine_id AND i.state = 'RUN' AND i.start_ts > a.start_ts
               )) AS effective_end_ts
        FROM downtime_annotations a
        """
    )
    op.execute(
        "CREATE INDEX ix_state_intervals_run_start ON state_intervals (machine_id, start_ts) WHERE state = 'RUN'"
    )
    op.execute("CREATE INDEX ix_programs_program_valid_from ON programs (program, valid_from)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_programs_program_valid_from")
    op.execute("DROP INDEX ix_state_intervals_run_start")
    op.execute("DROP VIEW downtime_annotations_resolved")
    op.execute("SELECT remove_compression_policy('telemetry')")
    op.execute("SELECT add_compression_policy('telemetry', INTERVAL '7 days')")
    op.execute("DROP TABLE oee_dirty")
