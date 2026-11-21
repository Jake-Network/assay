-- Step 3 (autoresearch) core extensions C1-C5: settings audit, blobs, partition-aware views,
-- partition best, lineage, in-flight work and knob history. Projects without settings.partition_key
-- see the same results as before (the partition expression is NULL for them).
SET search_path = benchbook;

-- benchbook_q copies are SELECT * snapshots; drop them so the base views can gain columns, then rebuild below.
DO $$
DECLARE v text;
BEGIN
  FOR v IN SELECT viewname FROM pg_views WHERE schemaname = 'benchbook_q' LOOP
    EXECUTE format('DROP VIEW benchbook_q.%I', v);
  END LOOP;
END $$;

-- C1: settings changes are audited in their own append-only table -------------------------------
CREATE TABLE settings_events (
  id          bigserial PRIMARY KEY,
  project_id  text NOT NULL REFERENCES projects(id),
  agent_id    text NOT NULL,
  patch       jsonb NOT NULL,
  settings    jsonb NOT NULL,          -- the settings after the patch was applied
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON settings_events (project_id, id);

-- C2: content-addressed blobs ------------------------------------------------------------------
CREATE TABLE blobs (
  project_id  text NOT NULL REFERENCES projects(id),
  sha256      text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  bytes       bytea NOT NULL,
  size        int NOT NULL,
  media_type  text NOT NULL DEFAULT 'application/octet-stream',
  author      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, sha256),
  CHECK (size = octet_length(bytes)),
  CHECK (sha256 = encode(sha256(bytes), 'hex'))
);

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['settings_events','blobs'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON benchbook.%I
                    FOR EACH ROW EXECUTE FUNCTION benchbook.forbid_mutation()', t || '_append_only', t);
  END LOOP;
END $$;
CREATE TRIGGER settings_events_notify AFTER INSERT ON settings_events
  FOR EACH ROW EXECUTE FUNCTION notify_insert();

-- C1: partition-aware ranking views (new column appended: partition) ----------------------------
CREATE OR REPLACE VIEW v_leaderboard AS
SELECT * FROM (
  SELECT e.project_id, e.id, e.author, e.task_id, e.config, e.code_ref, e.seed, e.metrics,
         e.primary_value, e.outcome, e.notes, e.created_at,
         row_number() OVER (PARTITION BY e.project_id, e.config ->> (p.settings ->> 'partition_key')
                            ORDER BY CASE WHEN p.metric_direction = 'min' THEN e.primary_value
                                          ELSE -e.primary_value END, e.created_at, e.id)::int AS rank,
         e.config ->> (p.settings ->> 'partition_key') AS partition
  FROM experiments_valid e JOIN projects p ON p.id = e.project_id
  WHERE e.run_status = 'ok' AND e.primary_value IS NOT NULL
) r WHERE rank <= 50;

CREATE OR REPLACE VIEW v_best AS
SELECT * FROM v_leaderboard WHERE rank = 1;

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
         min(e.created_at) AS first_at, max(e.created_at) AS last_at
  FROM experiments_valid e JOIN projects p ON p.id = e.project_id
  GROUP BY e.project_id, e.dedup_key
), latest AS (
  SELECT DISTINCT ON (project_id, dedup_key) project_id, dedup_key, id AS last_id, config, code_ref,
         outcome AS last_outcome, notes AS last_notes
  FROM experiments_valid ORDER BY project_id, dedup_key, created_at DESC, id DESC
)
SELECT g.*, l.last_id, l.config, l.code_ref, l.last_outcome, l.last_notes,
       CASE WHEN n_improved > 0 THEN 'improved'
            WHEN n_failed = attempts THEN 'failed'
            WHEN n_worse > 0 THEN 'worse'
            WHEN n_no_change > 0 THEN 'no_change'
            WHEN n_failed > 0 THEN 'failed'
            ELSE 'unscored' END AS summary_outcome,
       l.config ->> (p.settings ->> 'partition_key') AS partition
FROM g JOIN latest l USING (project_id, dedup_key) JOIN projects p ON p.id = g.project_id;

CREATE OR REPLACE VIEW v_progress AS
SELECT e.project_id, e.id, e.author, e.created_at, e.primary_value, e.outcome,
       CASE WHEN p.metric_direction = 'min'
            THEN min(e.primary_value) OVER w ELSE max(e.primary_value) OVER w END AS best_so_far,
       (row_number() OVER w)::int AS n,
       sum(coalesce(e.tokens, 0)) OVER w AS tokens_cum,
       e.config ->> (p.settings ->> 'partition_key') AS partition,
       e.seed
FROM experiments_valid e JOIN projects p ON p.id = e.project_id
WHERE e.run_status = 'ok' AND e.primary_value IS NOT NULL
WINDOW w AS (PARTITION BY e.project_id, e.config ->> (p.settings ->> 'partition_key') ORDER BY e.created_at, e.id);

-- C5: result claims (ml-research pack) and pending candidates per partition ----------------------
-- status 'verified': result claims whose latest decision is verified; status 'candidate': improved
-- first runs (seed 0 or NULL) that no result claim has judged yet. rank 1 is the best of each status.
CREATE VIEW v_partition_best AS
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
  WHERE e.outcome = 'improved' AND e.run_status = 'ok' AND coalesce(e.seed, 0) = 0
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

-- C5: lineage over experiment -derived_from-> experiment links --------------------------------------
CREATE VIEW v_lineage AS
WITH RECURSIVE par AS (
  SELECT DISTINCT ON (l.project_id, l.src_id) l.project_id, l.src_id AS id, l.dst_id AS parent_id
  FROM links l
  WHERE l.relation = 'derived_from' AND l.src_type = 'experiment' AND l.dst_type = 'experiment'
  ORDER BY l.project_id, l.src_id, l.created_at, l.id
), walk AS (
  SELECT e.project_id, e.id, NULL::uuid AS parent_id, 0 AS depth, e.id AS root_id
  FROM experiments e
  WHERE NOT EXISTS (SELECT 1 FROM par WHERE par.project_id = e.project_id AND par.id = e.id)
  UNION ALL
  SELECT par.project_id, par.id, par.parent_id, w.depth + 1, w.root_id
  FROM walk w JOIN par ON par.project_id = w.project_id AND par.parent_id = w.id
  WHERE w.depth < 1000
)
SELECT w.project_id, w.id, w.parent_id, w.depth, w.root_id, e.author, e.code_ref, e.seed,
       e.config ->> (p.settings ->> 'partition_key') AS partition,
       e.config -> 'changed' AS changed, e.config ->> 'description' AS description,
       e.run_status, e.outcome, e.primary_value, e.created_at
FROM walk w
JOIN experiments_valid e ON e.project_id = w.project_id AND e.id = w.id
JOIN projects p ON p.id = w.project_id;

-- C5: work in progress: claimed tasks with a live lease (intents are tagged 'intent') ---------------
CREATE VIEW v_inflight AS
SELECT t.project_id, t.id, t.title, t.tags, t.spec, t.lease_owner AS agent, t.priority,
       ('intent' = ANY (t.tags)) AS is_intent, t.spec ->> 'partition' AS partition,
       greatest(0, floor(extract(epoch FROM t.lease_expires_at - now())))::int AS lease_left_s,
       t.created_at, t.updated_at
FROM tasks t
WHERE t.status = 'claimed' AND t.lease_expires_at > now();

-- C5: knob history: one row per (partition, knob, new value) over first runs with config.changed ---
CREATE VIEW v_knob_history AS
WITH rs AS (
  SELECT l.project_id, l.src_id AS experiment_id, s.status
  FROM links l JOIN v_claim_status s ON s.project_id = l.project_id AND s.id = l.dst_id
  WHERE s.kind = 'result' AND l.relation = 'supports' AND l.src_type = 'experiment'
), x AS (
  SELECT e.project_id, e.config ->> (p.settings ->> 'partition_key') AS partition, k.key AS knob,
         k.value -> 1 AS value, k.value -> 0 AS old_value, e.outcome, e.created_at, rs.status AS result_status
  FROM experiments_valid e
  JOIN projects p ON p.id = e.project_id
  CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(e.config -> 'changed') = 'object'
                                     THEN e.config -> 'changed' ELSE '{}'::jsonb END) k
  LEFT JOIN rs ON rs.project_id = e.project_id AND rs.experiment_id = e.id
  WHERE coalesce(e.seed, 0) = 0
)
SELECT project_id, partition, knob, value,
       count(*)::int AS n,
       count(*) FILTER (WHERE outcome = 'improved')::int AS n_improved,
       count(*) FILTER (WHERE outcome = 'no_change')::int AS n_no_change,
       count(*) FILTER (WHERE outcome = 'worse')::int AS n_worse,
       count(*) FILTER (WHERE outcome = 'failed')::int AS n_failed,
       count(*) FILTER (WHERE result_status = 'verified')::int AS n_verified,
       count(*) FILTER (WHERE result_status = 'refuted')::int AS n_refuted,
       count(*) FILTER (WHERE result_status = 'inconclusive')::int AS n_inconclusive,
       max(created_at) AS last_at
FROM x GROUP BY project_id, partition, knob, value;

-- Rebuild the per-project reader copies of every v_* view (spec §7.3, DECISIONS.md).
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
