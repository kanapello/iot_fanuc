"""End-to-end skeleton: timeline, OEE, reference data and seed for the simulator.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None

DDL = """
ALTER TABLE telemetry
    ADD COLUMN aut_code integer, ADD COLUMN run_code integer,
    ADD COLUMN alarm_code integer, ADD COLUMN emergency_code integer;
ALTER TABLE sites ADD COLUMN timezone text NOT NULL DEFAULT 'Europe/Moscow';

CREATE TABLE state_rules (
    id serial PRIMARY KEY,
    name text NOT NULL,
    rules jsonb NOT NULL DEFAULT '{}',
    version integer NOT NULL DEFAULT 1
);
ALTER TABLE machines ADD COLUMN state_rules_id integer REFERENCES state_rules(id);
ALTER TABLE machines ADD COLUMN sort_order integer NOT NULL DEFAULT 0;

CREATE TABLE timeline_dirty (
    machine_id text PRIMARY KEY,
    from_ts timestamptz NOT NULL
);
CREATE TABLE state_intervals (
    id bigserial PRIMARY KEY,
    machine_id text NOT NULL,
    state text NOT NULL,
    start_ts timestamptz NOT NULL,
    end_ts timestamptz,
    program text,
    rules_version integer
);
CREATE INDEX ix_state_intervals_machine_id_start_ts ON state_intervals (machine_id, start_ts);
CREATE TABLE part_events (
    machine_id text NOT NULL,
    ts timestamptz NOT NULL,
    program text,
    delta integer NOT NULL,
    PRIMARY KEY (machine_id, ts)
);

CREATE TABLE shift_templates (
    id serial PRIMARY KEY,
    site_id text NOT NULL REFERENCES sites(id),
    name text NOT NULL,
    start_time time NOT NULL,
    end_time time NOT NULL,
    days_of_week integer[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',
    planned_breaks jsonb NOT NULL DEFAULT '[]',
    is_active boolean NOT NULL DEFAULT true
);
CREATE TABLE downtime_reasons (
    id serial PRIMARY KEY,
    parent_id integer REFERENCES downtime_reasons(id),
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    category text NOT NULL DEFAULT 'unplanned' CHECK (category IN ('planned', 'unplanned')),
    color text NOT NULL DEFAULT '#64748b',
    requires_comment boolean NOT NULL DEFAULT false,
    sort_order integer NOT NULL DEFAULT 0,
    is_active boolean NOT NULL DEFAULT true
);
CREATE TABLE scrap_reasons (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true
);
CREATE TABLE programs (
    id serial PRIMARY KEY,
    machine_id text REFERENCES machines(id),
    program text NOT NULL,
    ideal_cycle_time_s double precision NOT NULL CHECK (ideal_cycle_time_s > 0),
    parts_per_cycle integer NOT NULL DEFAULT 1 CHECK (parts_per_cycle > 0),
    valid_from timestamptz NOT NULL DEFAULT '2000-01-01T00:00:00Z'
);

CREATE TABLE downtime_annotations (
    id bigserial PRIMARY KEY,
    machine_id text NOT NULL,
    start_ts timestamptz NOT NULL,
    end_ts timestamptz,
    reason_id integer NOT NULL REFERENCES downtime_reasons(id),
    comment text,
    operator text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_downtime_annotations_machine_id_start_ts ON downtime_annotations (machine_id, start_ts);
CREATE TABLE scrap_records (
    id bigserial PRIMARY KEY,
    machine_id text NOT NULL,
    ts timestamptz NOT NULL,
    program text,
    qty integer NOT NULL CHECK (qty > 0),
    reason_id integer REFERENCES scrap_reasons(id),
    operator text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_scrap_records_machine_id_ts ON scrap_records (machine_id, ts);
CREATE TABLE operator_setup_marks (
    id bigserial PRIMARY KEY,
    machine_id text NOT NULL,
    start_ts timestamptz NOT NULL,
    end_ts timestamptz,
    operator text
);
CREATE INDEX ix_operator_setup_marks_machine_id_start_ts ON operator_setup_marks (machine_id, start_ts);

CREATE TABLE oee_hourly (
    machine_id text NOT NULL,
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    planned_time_s double precision NOT NULL,
    run_time_s double precision NOT NULL,
    idle_s double precision NOT NULL,
    stop_s double precision NOT NULL,
    alarm_s double precision NOT NULL,
    setup_s double precision NOT NULL,
    offline_s double precision NOT NULL,
    total_parts integer NOT NULL,
    good_parts integer NOT NULL,
    scrap_parts integer NOT NULL,
    ideal_time_s double precision NOT NULL,
    no_standard boolean NOT NULL DEFAULT false,
    availability double precision,
    performance double precision,
    quality double precision,
    oee double precision,
    calc_version integer NOT NULL DEFAULT 1,
    PRIMARY KEY (machine_id, period_start)
);
CREATE TABLE oee_shift (
    LIKE oee_hourly INCLUDING ALL,
    shift_name text NOT NULL DEFAULT '',
    planned_total_s double precision NOT NULL DEFAULT 0
);
"""

SEED = """
INSERT INTO sites (id, name, timezone) VALUES ('plant1', 'Площадка 1', 'Europe/Moscow') ON CONFLICT DO NOTHING;
INSERT INTO areas (id, site_id, name) VALUES (1, 'plant1', 'Фрезерный участок'), (2, 'plant1', 'Токарный участок');
SELECT setval('areas_id_seq', 2);
INSERT INTO collectors (id, site_id, name) VALUES ('edge-sim', 'plant1', 'Симулятор') ON CONFLICT DO NOTHING;
INSERT INTO machines (id, site_id, area_id, name, controller_type, collector_id, sort_order) VALUES
    ('sim01', 'plant1', 1, 'Фрезерный 01', 'Fanuc 0i-MD (симулятор)', 'edge-sim', 1),
    ('sim02', 'plant1', 1, 'Фрезерный 02', 'Fanuc 0i-MD (симулятор)', 'edge-sim', 2),
    ('sim03', 'plant1', 1, 'Фрезерный 03', 'Fanuc 0i-MD (симулятор)', 'edge-sim', 3),
    ('sim04', 'plant1', 2, 'Токарный 04', 'Fanuc 0i-TD (симулятор)', 'edge-sim', 4),
    ('sim05', 'plant1', 2, 'Токарный 05', 'Fanuc 0i-TD (симулятор)', 'edge-sim', 5),
    ('sim06', 'plant1', 2, 'Токарный 06', 'Fanuc 0i-TD (симулятор)', 'edge-sim', 6)
ON CONFLICT DO NOTHING;

INSERT INTO state_rules (id, name, rules) VALUES (1, 'По умолчанию', '{
    "offline_timeout_s": 30, "idle_to_stop_s": 120, "min_interval_s": 5,
    "run_modes": ["MEM", "RMT", "DNC"], "setup_modes": ["EDIT", "MDI", "JOG", "HND"]}');
SELECT setval('state_rules_id_seq', 1);

INSERT INTO shift_templates (site_id, name, start_time, end_time, planned_breaks) VALUES
    ('plant1', 'День', '08:00', '20:00',
     '[{"name": "Обед", "start": "12:00", "end": "12:45"}, {"name": "Перерыв", "start": "16:00", "end": "16:15"}]'),
    ('plant1', 'Ночь', '20:00', '08:00',
     '[{"name": "Обед", "start": "00:00", "end": "00:45"}, {"name": "Перерыв", "start": "04:00", "end": "04:15"}]');

INSERT INTO downtime_reasons (id, parent_id, code, name, category, color, sort_order) VALUES
    (1, NULL, 'ORG', 'Организационные', 'unplanned', '#f59e0b', 1),
    (2, NULL, 'TECH', 'Технические', 'unplanned', '#ef4444', 2),
    (3, NULL, 'PLAN', 'Плановые', 'planned', '#3b82f6', 3),
    (10, 1, 'NO_MATERIAL', 'Нет заготовок', 'unplanned', '#f59e0b', 1),
    (11, 1, 'NO_OPERATOR', 'Нет оператора', 'unplanned', '#fb923c', 2),
    (12, 1, 'NO_TASK', 'Нет задания / программы', 'unplanned', '#fbbf24', 3),
    (13, 1, 'QC_WAIT', 'Ожидание ОТК', 'unplanned', '#d97706', 4),
    (20, 2, 'BREAKDOWN', 'Поломка станка', 'unplanned', '#ef4444', 1),
    (21, 2, 'TOOL', 'Замена / поломка инструмента', 'unplanned', '#f87171', 2),
    (22, 2, 'REPAIR_WAIT', 'Ожидание ремонта', 'unplanned', '#b91c1c', 3),
    (30, 3, 'CHANGEOVER', 'Переналадка', 'planned', '#3b82f6', 1),
    (31, 3, 'MAINTENANCE', 'Плановое ТО', 'planned', '#60a5fa', 2),
    (32, 3, 'NO_ORDERS', 'Нет загрузки', 'planned', '#93c5fd', 3);
UPDATE downtime_reasons SET requires_comment = true WHERE code = 'BREAKDOWN';
SELECT setval('downtime_reasons_id_seq', 100);

INSERT INTO scrap_reasons (code, name) VALUES
    ('SIZE', 'Размер не в допуске'), ('SURFACE', 'Дефект поверхности'),
    ('MATERIAL', 'Дефект заготовки'), ('SETUP', 'Наладочный брак');

-- Cycle norms (seconds per part) for the simulator programs; machine_id NULL = any machine.
INSERT INTO programs (machine_id, program, ideal_cycle_time_s, parts_per_cycle) VALUES
    (NULL, 'O1234', 52, 1), (NULL, 'O1250', 100, 1), (NULL, 'O2001', 36, 1), (NULL, 'O3100', 70, 1),
    (NULL, 'O4000', 48, 1), (NULL, 'O5005', 25, 1), (NULL, 'O6001', 48, 1), (NULL, 'O6002', 85, 1);
"""


def _run(script: str) -> None:
    for statement in script.split(";\n"):
        if statement.strip():
            op.execute(statement)


def upgrade() -> None:
    _run(DDL)
    _run(SEED)


def downgrade() -> None:
    for table in (
        "oee_shift", "oee_hourly", "operator_setup_marks", "scrap_records", "downtime_annotations", "programs",
        "scrap_reasons", "downtime_reasons", "shift_templates", "part_events", "state_intervals", "timeline_dirty",
    ):  # fmt: skip
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE machines DROP COLUMN state_rules_id, DROP COLUMN sort_order")
    op.execute("DROP TABLE state_rules")
    op.execute("ALTER TABLE sites DROP COLUMN timezone")
    op.execute(
        "ALTER TABLE telemetry DROP COLUMN aut_code, DROP COLUMN run_code, "
        "DROP COLUMN alarm_code, DROP COLUMN emergency_code"
    )
