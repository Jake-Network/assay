SET search_path = assay;

-- Match the SDK fallback for legacy non-numeric partition epsilons.
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
         coalesce(
           CASE WHEN jsonb_typeof(p.settings -> 'partitions' -> g.part -> 'epsilon') = 'number'
                THEN (p.settings -> 'partitions' -> g.part ->> 'epsilon')::double precision
           END,
           p.improve_epsilon
         ) AS eps
  FROM g g2
  WHERE g2.project_id = g.project_id AND g2.part IS NOT DISTINCT FROM g.part AND g2.dedup_key <> g.dedup_key
) ob;

-- Rerun decisions from older releases can contain a result claim inconsistent with their evidence.
CREATE OR REPLACE VIEW v_partition_best AS
WITH res AS (
  SELECT c.project_id, c.payload ->> 'partition' AS partition, 'verified'::text AS status,
         'claim'::text AS ref_type, c.id AS ref_id, c.payload ->> 'code_ref' AS code_ref,
         CASE WHEN s.last_oracle = 'rerun' THEN stats.mean::double precision
              ELSE (c.payload ->> 'mean')::double precision END AS mean,
         CASE WHEN s.last_oracle = 'rerun' THEN stats.std::double precision
              ELSE (c.payload ->> 'std')::double precision END AS std,
         CASE WHEN s.last_oracle = 'rerun' THEN stats.n ELSE (c.payload ->> 'n')::int END AS n,
         CASE WHEN s.last_oracle = 'rerun' THEN original.author
              ELSE coalesce(c.payload ->> 'origin_author', c.author) END AS author,
         CASE WHEN s.last_oracle = 'rerun' THEN original.id ELSE (c.payload ->> 'origin')::uuid END AS origin_id,
         s.decided_at AS created_at
  FROM claims c JOIN v_claim_status s ON s.project_id = c.project_id AND s.id = c.id
  JOIN projects p ON p.id = c.project_id
  LEFT JOIN LATERAL (
    SELECT d.evidence_ids, d.oracle_detail->'rerun_check' AS recorded_check,
           CASE WHEN d.oracle_detail #>> '{rerun_check,original}' ~
                     '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (d.oracle_detail #>> '{rerun_check,original}')::uuid END AS original_id
    FROM decisions d
    WHERE d.project_id = c.project_id AND d.target_type = 'claim' AND d.target_id = c.id
      AND d.created_at = s.decided_at AND d.new_status = 'verified' AND d.oracle = 'rerun'
    ORDER BY d.id DESC LIMIT 1
  ) decision ON s.last_oracle = 'rerun'
  LEFT JOIN LATERAL (
    SELECT e.id, e.author, e.code_ref, e.config ->> coalesce(p.settings->>'partition_key', 'partition') AS partition
    FROM experiments e
    WHERE e.project_id = c.project_id AND e.run_status = 'ok'
      AND e.id = ANY(CASE WHEN decision.original_id IS NULL THEN decision.evidence_ids
                          ELSE ARRAY[decision.original_id] END)
    ORDER BY e.created_at, e.id LIMIT 1
  ) original ON s.last_oracle = 'rerun'
  LEFT JOIN LATERAL (
    SELECT avg(e.primary_value::numeric) AS mean, stddev_samp(e.primary_value::numeric) AS std, count(*)::int AS n,
           avg(e.primary_value::numeric) FILTER (WHERE e.author <> original.author) AS independent_mean,
           bool_or(e.author = original.author AND e.id <> original.id) AS self_reruns
    FROM experiments e
    WHERE e.project_id = c.project_id AND e.id = ANY(decision.evidence_ids)
      AND e.run_status = 'ok' AND e.primary_value IS NOT NULL
  ) stats ON s.last_oracle = 'rerun'
  WHERE c.kind = 'result' AND s.status = 'verified' AND NOT s.evidence_invalidated
    AND (s.last_oracle <> 'rerun' OR (
      c.payload ->> 'partition' = original.partition AND c.payload ->> 'code_ref' = original.code_ref
      AND (c.payload ->> 'origin' IS NULL OR lower(c.payload ->> 'origin') = original.id::text)
      AND abs(stats.mean - (c.payload ->> 'mean')::numeric) <=
          greatest(greatest(abs(stats.mean), abs((c.payload ->> 'mean')::numeric)) * 1e-12, 1e-15)
      -- A historical improvement may have been accepted using the original author's extra runs.
      -- Recheck independence when the decision preserved the threshold actually used.
      AND CASE WHEN stats.self_reruns AND jsonb_typeof(decision.recorded_check->'baseline') = 'number'
                                     AND jsonb_typeof(decision.recorded_check->'epsilon') = 'number' THEN
                  CASE WHEN p.metric_direction = 'min' THEN
                    stats.independent_mean < (decision.recorded_check->>'baseline')::numeric
                                            - (decision.recorded_check->>'epsilon')::numeric
                  ELSE
                    stats.independent_mean > (decision.recorded_check->>'baseline')::numeric
                                            + (decision.recorded_check->>'epsilon')::numeric
                  END
               ELSE true
          END
    ))
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
