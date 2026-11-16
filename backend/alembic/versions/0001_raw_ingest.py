"""Reference tables and raw hypertables for ingest (stage 1).

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "sites",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
    )
    op.create_table(
        "areas",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("site_id", sa.Text, sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
    )
    op.create_table(
        "collectors",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("site_id", sa.Text, sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version", sa.Text),
        sa.Column("last_heartbeat", TSTZ),
    )
    op.create_table(
        "machines",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("site_id", sa.Text, sa.ForeignKey("sites.id"), nullable=False),
        sa.Column("area_id", sa.Integer, sa.ForeignKey("areas.id")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("controller_type", sa.Text),
        sa.Column("collector_id", sa.Text, sa.ForeignKey("collectors.id")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TSTZ, nullable=False, server_default=sa.func.now()),
    )

    # Raw data. The key is (msg_id, ts): unique indexes on a hypertable must
    # include the partitioning column (docs/adr/0001).
    op.create_table(
        "telemetry",
        sa.Column("msg_id", sa.Uuid, nullable=False),
        sa.Column("ts", TSTZ, nullable=False),
        sa.Column("machine_id", sa.Text, nullable=False),
        sa.Column("collector_id", sa.Text, nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("connected", sa.Boolean, nullable=False),
        sa.Column("mode", sa.Text),
        sa.Column("run", sa.Text),
        sa.Column("motion", sa.Boolean),
        sa.Column("emergency", sa.Boolean),
        sa.Column("alarm", sa.Boolean),
        sa.Column("program", sa.Text),
        sa.Column("program_name", sa.Text),
        sa.Column("feed_actual", sa.Float),
        sa.Column("spindle_speed", sa.Float),
        sa.Column("spindle_load", sa.Float),
        sa.Column("feed_override", sa.Float),
        sa.Column("spindle_override", sa.Float),
        sa.Column("part_count", sa.BigInteger),
        sa.Column("received_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("msg_id", "ts", name="pk_telemetry"),
    )
    op.execute("SELECT create_hypertable('telemetry', by_range('ts', INTERVAL '1 day'))")
    op.create_index("ix_telemetry_machine_id_ts", "telemetry", ["machine_id", "ts"])
    op.execute(
        "ALTER TABLE telemetry SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'machine_id', timescaledb.compress_orderby = 'ts DESC')"
    )
    op.execute("SELECT add_compression_policy('telemetry', INTERVAL '7 days')")
    op.execute("SELECT add_retention_policy('telemetry', INTERVAL '2 years')")

    op.create_table(
        "machine_events",
        sa.Column("msg_id", sa.Uuid, nullable=False),
        sa.Column("ts", TSTZ, nullable=False),
        sa.Column("machine_id", sa.Text, nullable=False),
        sa.Column("collector_id", sa.Text, nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("code", sa.Text),
        sa.Column("message", sa.Text),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("received_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("msg_id", "ts", name="pk_machine_events"),
    )
    op.execute("SELECT create_hypertable('machine_events', by_range('ts', INTERVAL '7 days'))")
    op.create_index("ix_machine_events_machine_id_ts", "machine_events", ["machine_id", "ts"])
    op.execute("SELECT add_retention_policy('machine_events', INTERVAL '3 years')")

    op.create_table(
        "ingest_rejects",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("received_at", TSTZ, nullable=False, server_default=sa.func.now()),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("payload", sa.Text, nullable=False),
    )
    op.create_index("ix_ingest_rejects_received_at", "ingest_rejects", ["received_at"])


def downgrade() -> None:
    for table in ("ingest_rejects", "machine_events", "telemetry", "machines", "collectors", "areas", "sites"):
        op.drop_table(table)
