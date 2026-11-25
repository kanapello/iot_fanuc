-- Removes the demo organization that the migrations create ("Демо-предприятие", code "demo") with its
-- sites, areas, collectors, machines and references: make prod-drop-demo (docs/runbooks/cloud-install.md).
-- Refuses when its machines have any data from collectors. One transaction: all or nothing.
\set ON_ERROR_STOP on
BEGIN;
DO $$
DECLARE
    v_org bigint;
    v_sites bigint[];
    v_machines bigint[];
BEGIN
    SELECT id INTO v_org FROM organizations WHERE code = 'demo';
    IF v_org IS NULL THEN
        RAISE NOTICE 'there is no demo organization: nothing to do';
        RETURN;
    END IF;
    SELECT COALESCE(array_agg(id), '{}') INTO v_sites FROM sites WHERE org_id = v_org;
    SELECT COALESCE(array_agg(id), '{}') INTO v_machines FROM machines WHERE site_id = ANY (v_sites);
    IF EXISTS (SELECT 1 FROM telemetry WHERE machine_id = ANY (v_machines))
        OR EXISTS (SELECT 1 FROM machine_events WHERE machine_id = ANY (v_machines)) THEN
        RAISE EXCEPTION 'the demo organization has data from collectors: it is not removed';
    END IF;

    -- derived data (rebuilt from raw data, of which there is none)
    DELETE FROM cycles WHERE machine_id = ANY (v_machines);
    DELETE FROM state_intervals WHERE machine_id = ANY (v_machines);
    DELETE FROM part_events WHERE machine_id = ANY (v_machines);
    DELETE FROM oee_hourly WHERE machine_id = ANY (v_machines);
    DELETE FROM oee_shift WHERE machine_id = ANY (v_machines);
    DELETE FROM timeline_dirty WHERE machine_id = ANY (v_machines);
    DELETE FROM oee_dirty WHERE machine_id = ANY (v_machines);
    -- what people entered for the demo machines
    DELETE FROM downtime_annotations WHERE machine_id = ANY (v_machines);
    DELETE FROM scrap_records WHERE machine_id = ANY (v_machines);
    DELETE FROM operator_setup_marks WHERE machine_id = ANY (v_machines);
    DELETE FROM ballbar_files WHERE test_id IN (SELECT id FROM ballbar_tests WHERE machine_id = ANY (v_machines));
    DELETE FROM ballbar_results WHERE test_id IN (SELECT id FROM ballbar_tests WHERE machine_id = ANY (v_machines));
    DELETE FROM ballbar_traces WHERE test_id IN (SELECT id FROM ballbar_tests WHERE machine_id = ANY (v_machines));
    DELETE FROM ballbar_tests WHERE machine_id = ANY (v_machines);
    DELETE FROM collision_marks WHERE machine_id = ANY (v_machines);
    DELETE FROM repair_records WHERE machine_id = ANY (v_machines);
    DELETE FROM repair_request_history
        WHERE request_id IN (SELECT id FROM repair_requests WHERE machine_id = ANY (v_machines));
    DELETE FROM repair_requests WHERE machine_id = ANY (v_machines);
    DELETE FROM maintenance_records
        WHERE plan_id IN (SELECT id FROM maintenance_plans WHERE machine_id = ANY (v_machines));
    DELETE FROM maintenance_plans WHERE machine_id = ANY (v_machines);
    -- the structure and the references of the organization
    DELETE FROM programs WHERE machine_id = ANY (v_machines) OR org_id = v_org;
    DELETE FROM machines WHERE id = ANY (v_machines);  -- device tokens of the machines go with them
    DELETE FROM collectors WHERE site_id = ANY (v_sites);
    DELETE FROM areas WHERE site_id = ANY (v_sites);
    DELETE FROM state_rules WHERE org_id = v_org;
    DELETE FROM shift_templates WHERE org_id = v_org;
    DELETE FROM scrap_reasons WHERE org_id = v_org;
    DELETE FROM downtime_reasons WHERE org_id = v_org AND parent_id IS NOT NULL;
    DELETE FROM downtime_reasons WHERE org_id = v_org;
    DELETE FROM sites WHERE id = ANY (v_sites);  -- memberships on the sites go with them
    DELETE FROM organizations WHERE id = v_org;
    RAISE NOTICE 'the demo organization is removed: % sites, % machines', cardinality(v_sites), cardinality(v_machines);
END
$$;
COMMIT;
