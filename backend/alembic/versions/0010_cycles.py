"""Cycle split: derived cycles between part events, and the pause threshold of a machine.

``cycles`` is derived data: it is rebuilt together with the timeline from the part events and the
state intervals. The history already stored is rebuilt by the worker: every machine with part events
gets a timeline mark from its first part.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-24
"""

from __future__ import annotations

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None

UPGRADE = (
    """
    CREATE TABLE cycles (
        machine_id bigint NOT NULL REFERENCES machines(id),
        end_ts timestamptz NOT NULL,
        start_ts timestamptz NOT NULL,
        program text,
        parts integer NOT NULL CHECK (parts > 0),
        machine_s double precision NOT NULL,
        gap_s double precision NOT NULL,
        idle_s double precision NOT NULL,
        stop_s double precision NOT NULL,
        alarm_s double precision NOT NULL,
        setup_s double precision NOT NULL,
        offline_s double precision NOT NULL,
        kind text NOT NULL CHECK (kind IN ('normal', 'setup', 'incomplete')),
        PRIMARY KEY (machine_id, end_ts),
        CHECK (end_ts > start_ts)
    )
    """,
    # null = three times the median machine time of the program, at least 10 minutes
    "ALTER TABLE machines ADD COLUMN cycle_pause_threshold_s double precision CHECK (cycle_pause_threshold_s > 0)",
    "INSERT INTO timeline_dirty (machine_id, from_ts) SELECT machine_id, min(ts) FROM part_events GROUP BY machine_id "
    "ON CONFLICT (machine_id) DO UPDATE SET from_ts = LEAST(timeline_dirty.from_ts, EXCLUDED.from_ts)",
)

DOWNGRADE = (
    "ALTER TABLE machines DROP COLUMN cycle_pause_threshold_s",
    "DROP TABLE cycles",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
