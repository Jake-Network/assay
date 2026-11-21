-- Read-only role for benchbook_sql (spec §7.3). The reader sees only per-project v_* views in
-- schema benchbook_q, which filter by the session setting benchbook.project. No base table access.
SET search_path = benchbook;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'benchbook_reader') THEN
    CREATE ROLE benchbook_reader LOGIN;  -- LOGIN so benchbook_sql can connect as it (session user = reader)
  END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS benchbook_q;
REVOKE ALL ON SCHEMA benchbook FROM benchbook_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA benchbook FROM benchbook_reader;

DO $$
DECLARE v text;
BEGIN
  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'benchbook' AND viewname LIKE 'v\_%' LOOP
    EXECUTE format('CREATE VIEW benchbook_q.%I AS SELECT * FROM benchbook.%I
                    WHERE project_id = current_setting(''benchbook.project'')', v, v);
  END LOOP;
END $$;

GRANT USAGE ON SCHEMA benchbook_q TO benchbook_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA benchbook_q TO benchbook_reader;

-- the connecting user must be able to SET ROLE benchbook_reader
DO $$
BEGIN
  EXECUTE format('GRANT benchbook_reader TO %I', current_user);
EXCEPTION WHEN others THEN NULL;  -- already granted, or superuser
END $$;
