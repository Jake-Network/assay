-- Read-only role for assay_sql (spec §7.3). The reader sees only per-project v_* views in
-- schema assay_q, which filter by the session setting assay.project. No base table access.
SET search_path = assay;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'assay_reader') THEN
    CREATE ROLE assay_reader LOGIN;  -- LOGIN so assay_sql can connect as it (session user = reader)
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS assay_q;
REVOKE ALL ON SCHEMA assay FROM assay_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA assay FROM assay_reader;

DO $$
DECLARE v text;
BEGIN
  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'assay' AND viewname LIKE 'v\_%' LOOP
    EXECUTE format('CREATE VIEW assay_q.%I AS SELECT * FROM assay.%I
                    WHERE project_id = current_setting(''assay.project'')', v, v);
  END LOOP;
END $$;

GRANT USAGE ON SCHEMA assay_q TO assay_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA assay_q TO assay_reader;

-- the connecting user must be able to SET ROLE assay_reader
DO $$
BEGIN
  EXECUTE format('GRANT assay_reader TO %I', current_user);
EXCEPTION WHEN others THEN NULL;  -- already granted, or superuser
END $$;
