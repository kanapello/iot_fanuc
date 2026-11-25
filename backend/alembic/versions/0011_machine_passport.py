"""QR passport of a machine: a permanent random code (8 characters) behind the link /m/<code>.

The code comes from a database function, so every machine gets one: those created by an
administrator and those registered by ingest from incoming data. It can be reissued (the old
sticker stops working). The alphabet has no look-alikes (no 0/O, no 1/I); the randomness comes
from gen_random_uuid(), whose bytes 0-5 and 10-11 are random in a version 4 UUID.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-24
"""

from __future__ import annotations

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

UPGRADE = (
    """
    CREATE FUNCTION new_passport_code() RETURNS text LANGUAGE plpgsql VOLATILE AS $$
    DECLARE
        alphabet constant text := '23456789ABCDEFGHJKLMNPQRSTUVWXYZ';
        positions constant int[] := ARRAY[0, 1, 2, 3, 4, 5, 10, 11];
        random_bytes bytea;
        candidate text;
    BEGIN
        LOOP
            random_bytes := decode(replace(gen_random_uuid()::text, '-', ''), 'hex');
            candidate := '';
            FOR i IN 1..8 LOOP
                candidate := candidate || substr(alphabet, 1 + get_byte(random_bytes, positions[i]) % 32, 1);
            END LOOP;
            EXIT WHEN NOT EXISTS (SELECT 1 FROM machines m WHERE m.passport_code = candidate);
        END LOOP;
        RETURN candidate;
    END
    $$
    """,
    "ALTER TABLE machines ADD COLUMN passport_code text",
    "UPDATE machines SET passport_code = new_passport_code()",
    "ALTER TABLE machines ALTER COLUMN passport_code SET NOT NULL",
    "ALTER TABLE machines ALTER COLUMN passport_code SET DEFAULT new_passport_code()",
    "ALTER TABLE machines ADD CONSTRAINT uq_machines_passport_code UNIQUE (passport_code)",
)

DOWNGRADE = (
    "ALTER TABLE machines DROP COLUMN passport_code",
    "DROP FUNCTION new_passport_code()",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
