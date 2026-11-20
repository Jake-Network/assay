-- Review fixes (2026-11): append-only also against TRUNCATE, agents keep their role, assay_sql cannot
-- call set_config, seed NULL and -1 are different runs, v_tried summarises replicated configs by their
-- mean, and a candidate is the first run of a config whatever its seed.
SET search_path = assay;

-- C2: TRUNCATE skips row triggers; forbid it per statement on every ledger table ------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['projects','agents','tasks','task_events','experiments','claims','sources','links',
                           'decisions','brief_log','dedup_hits','settings_events','blobs'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON assay.%I
                    FOR EACH STATEMENT EXECUTE FUNCTION assay.forbid_mutation()', t || '_no_truncate', t);
  END LOOP;
END $$;

-- C2: check_decider_role trusts agents.role, so an agent row is never re-roled or deleted ----------
CREATE FUNCTION forbid_agent_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' OR NEW.role IS DISTINCT FROM OLD.role OR NEW.id IS DISTINCT FROM OLD.id
     OR NEW.project_id IS DISTINCT FROM OLD.project_id THEN
    RAISE EXCEPTION 'assay: % of agent %/% is forbidden; an agent keeps its name and role (P2)',
      TG_OP, OLD.project_id, OLD.id USING ERRCODE = 'restrict_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER agents_fixed_role BEFORE UPDATE OR DELETE ON agents
  FOR EACH ROW EXECUTE FUNCTION forbid_agent_change();

-- C4: the reader must not change assay.project or its role from inside a query. Needs ownership of
-- pg_catalog.set_config (a superuser); elsewhere the regex and the post-query check in Ledger.sql remain.
DO $$
BEGIN
  REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) FROM PUBLIC;
  EXECUTE format('GRANT EXECUTE ON FUNCTION pg_catalog.set_config(text, text, boolean) TO %I', current_user);
EXCEPTION WHEN insufficient_privilege THEN
  RAISE NOTICE 'assay: could not revoke set_config from PUBLIC (not a superuser); assay_sql relies on its query filter';
END $$;

-- C7: seed NULL and seed -1 are different runs ------------------------------------------------------
DROP INDEX experiments_no_exact_dup;
CREATE UNIQUE INDEX experiments_no_exact_dup ON experiments (project_id, dedup_key, seed)
  WHERE run_status = 'ok' AND seed IS NOT NULL;
CREATE UNIQUE INDEX experiments_no_exact_dup_noseed ON experiments (project_id, dedup_key)
  WHERE run_status = 'ok' AND seed IS NULL;

-- C5: a config run with two or more seeds is summarised by its mean against the best mean of the other
-- configs in its partition, so one lucky seed no longer labels it 'improved' --------------------------
CREATE OR REPLACE VIEW v_tried AS
WITH g AS (
  SELECT e.project_id, e.dedup_key,
         count(*)::int AS attempts,
         count(DISTINCT e.seed) FILTER (WHERE e.run_status = 'ok')::int AS seeds,
         count(*) FILTER (WHERE e.run_status = 'ok')::int AS ok_runs,
         avg(e.primary_value) AS mean_value,
         stddev_samp(e.primary_value) AS std_value,
         CASE WHEN max(p.metric_direction) = 'min' THEN min(e.primary_value)
              ELSE max(e.primary_value) END AS best_value,
         count(*) FILTER (WHERE e.outcome = 'improved')::int AS n_improved,
         count(*) FILTER (WHERE e.outcome = 'no_change')::int AS n_no_change,
         count(*) FILTER (WHERE e.outcome = 'worse')::int AS n_worse,
         count(*) FILTER (WHERE e.outcome = 'failed')::int AS n_failed,
         count(*) FILTER (WHERE e.outcome = 'unscored')::int AS n_unscored,
         count(DISTINCT e.author)::int AS authors,
         min(e.created_at) AS first_at, max(e.created_at) AS last_at,
         max(e.config ->> (p.settings ->> 'partition_key')) AS part
  FROM experiments_valid e JOIN projects p ON p.id = e.project_id
  GROUP BY e.project_id, e.dedup_key
), latest AS (
  SELECT DISTINCT ON (project_id, dedup_key) project_id, dedup_key, id AS last_id, config, code_ref,
         outcome AS last_outcome, notes AS last_notes
  FROM experiments_valid ORDER BY project_id, dedup_key, created_at DESC, id DESC
)
SELECT g.project_id, g.dedup_key, g.attempts, g.seeds, g.ok_runs, g.mean_value, g.std_value, g.best_value,
       g.n_improved, g.n_no_change, g.n_worse, g.n_failed, g.n_unscored, g.authors, g.first_at, g.last_at,
       l.last_id, l.config, l.code_ref, l.last_outcome, l.last_notes,
       CASE WHEN g.seeds >= 2 AND g.mean_value IS NOT NULL AND ob.v IS NOT NULL THEN
              CASE WHEN (p.metric_direction = 'min' AND g.mean_value < ob.v - ob.eps)
                     OR (p.metric_direction = 'max' AND g.mean_value > ob.v + ob.eps) THEN 'improved'
                   WHEN (p.metric_direction = 'min' AND g.mean_value > ob.v + ob.eps)
                     OR (p.metric_direction = 'max' AND g.mean_value < ob.v - ob.eps) THEN 'worse'
                   ELSE 'no_change' END
            WHEN n_improved > 0 THEN 'improved'
            WHEN n_failed = attempts THEN 'failed'
            WHEN n_worse > 0 THEN 'worse'
            WHEN n_no_change > 0 THEN 'no_change'
            WHEN n_failed > 0 THEN 'failed'
            ELSE 'unscored' END AS summary_outcome,
       l.config ->> (p.settings ->> 'partition_key') AS partition
FROM g JOIN latest l USING (project_id, dedup_key) JOIN projects p ON p.id = g.project_id
CROSS JOIN LATERAL (
  SELECT CASE WHEN p.metric_direction = 'min' THEN min(g2.mean_value) ELSE max(g2.mean_value) END AS v,
         coalesce((p.settings -> 'partitions' -> g.part ->> 'epsilon')::double precision, p.improve_epsilon) AS eps
  FROM g g2
  WHERE g2.project_id = g.project_id AND g2.part IS NOT DISTINCT FROM g.part AND g2.dedup_key <> g.dedup_key
) ob;

-- C7: candidates are first runs of a config whatever their seed ------------------------------------
CREATE OR REPLACE VIEW v_partition_best AS
WITH res AS (
  SELECT c.project_id, c.payload ->> 'partition' AS partition, 'verified'::text AS status,
         'claim'::text AS ref_type, c.id AS ref_id, c.payload ->> 'code_ref' AS code_ref,
         (c.payload ->> 'mean')::double precision AS mean,
         (c.payload ->> 'std')::double precision AS std,
         (c.payload ->> 'n')::int AS n,
         coalesce(c.payload ->> 'origin_author', c.author) AS author,
         (c.payload ->> 'origin')::uuid AS origin_id,
         s.decided_at AS created_at
  FROM claims c JOIN v_claim_status s ON s.project_id = c.project_id AND s.id = c.id
  WHERE c.kind = 'result' AND s.status = 'verified' AND NOT s.evidence_invalidated
), judged AS (
  SELECT DISTINCT l.project_id, l.src_id FROM links l
  JOIN claims c ON c.project_id = l.project_id AND c.id = l.dst_id AND c.kind = 'result'
  WHERE l.relation = 'supports' AND l.src_type = 'experiment'
), cand AS (
  SELECT e.project_id, e.config ->> (p.settings ->> 'partition_key') AS partition, 'candidate'::text AS status,
         'experiment'::text AS ref_type, e.id AS ref_id, e.code_ref, e.primary_value AS mean,
         NULL::double precision AS std, 1 AS n, e.author, e.id AS origin_id, e.created_at
  FROM experiments_valid e JOIN projects p ON p.id = e.project_id
  WHERE e.outcome = 'improved' AND e.run_status = 'ok'
    -- the first ok run of its config, whatever its seed (was: seed 0 or NULL only)
    AND NOT EXISTS (SELECT 1 FROM experiments_valid f WHERE f.project_id = e.project_id AND f.dedup_key = e.dedup_key
                    AND f.run_status = 'ok' AND (f.created_at, f.id) < (e.created_at, e.id))
    AND p.settings ? 'partition_key'
    AND NOT EXISTS (SELECT 1 FROM judged j WHERE j.project_id = e.project_id AND j.src_id = e.id)
), u AS (
  SELECT * FROM res UNION ALL SELECT * FROM cand
)
SELECT u.project_id, u.partition, u.status, u.ref_type, u.ref_id, u.code_ref, u.mean, u.std, u.n, u.author,
       u.origin_id, u.created_at,
       row_number() OVER (PARTITION BY u.project_id, u.partition, u.status
                          ORDER BY CASE WHEN p.metric_direction = 'min' THEN u.mean ELSE -u.mean END,
                                   u.created_at, u.ref_id)::int AS rank
FROM u JOIN projects p ON p.id = u.project_id;
