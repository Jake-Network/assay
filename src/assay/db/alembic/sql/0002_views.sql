-- Read views (spec §5). Every experiment-based view excludes invalidated experiments (I7).
SET search_path = assay;

-- internal helper (not exposed to assay_reader)
CREATE VIEW experiments_valid AS
SELECT e.* FROM experiments e
WHERE NOT EXISTS (SELECT 1 FROM decisions d
                  WHERE d.target_type = 'experiment' AND d.target_id = e.id
                    AND d.new_status = 'invalidated');

CREATE VIEW v_claim_status AS
WITH last_dec AS (
  SELECT DISTINCT ON (target_id) target_id, new_status, oracle, decided_by, created_at
  FROM decisions WHERE target_type = 'claim'
  ORDER BY target_id, created_at DESC, id DESC
), inval AS (
  SELECT DISTINCT target_id AS id FROM decisions WHERE new_status = 'invalidated'
), ev AS (
  SELECT l.dst_id AS claim_id, l.relation, l.src_type, l.src_id,
         (l.src_type = 'experiment' AND l.src_id IN (SELECT id FROM inval)) AS invalid,
         coalesce(e.author, c.author, s.author, l.author) AS src_author
  FROM links l
  LEFT JOIN experiments e ON l.src_type = 'experiment' AND e.id = l.src_id
  LEFT JOIN claims c ON l.src_type = 'claim' AND c.id = l.src_id
  LEFT JOIN sources s ON l.src_type = 'source' AND s.id = l.src_id
  WHERE l.dst_type = 'claim' AND l.relation IN ('supports','refutes')
)
SELECT c.project_id, c.id, c.kind, c.statement, c.tags, c.author, c.task_id, c.created_at,
       coalesce(d.new_status, 'proposed') AS status,
       d.oracle AS last_oracle, d.decided_by AS last_decided_by, d.created_at AS decided_at,
       (SELECT count(*) FROM ev WHERE ev.claim_id = c.id AND relation = 'supports' AND NOT invalid)::int AS n_support,
       (SELECT count(*) FROM ev WHERE ev.claim_id = c.id AND relation = 'refutes' AND NOT invalid)::int AS n_refute,
       (SELECT count(DISTINCT src_author) FROM ev
         WHERE ev.claim_id = c.id AND relation = 'supports' AND NOT invalid)::int AS n_support_authors,
       (EXISTS (SELECT 1 FROM ev WHERE ev.claim_id = c.id AND relation = 'supports' AND invalid)
        AND NOT EXISTS (SELECT 1 FROM ev WHERE ev.claim_id = c.id AND relation = 'supports' AND NOT invalid))
         AS evidence_invalidated
FROM claims c LEFT JOIN last_dec d ON d.target_id = c.id;

CREATE VIEW v_leaderboard AS
SELECT * FROM (
  SELECT e.project_id, e.id, e.author, e.task_id, e.config, e.code_ref, e.seed, e.metrics,
         e.primary_value, e.outcome, e.notes, e.created_at,
         row_number() OVER (PARTITION BY e.project_id
                            ORDER BY CASE WHEN p.metric_direction = 'min' THEN e.primary_value
                                          ELSE -e.primary_value END, e.created_at, e.id)::int AS rank
  FROM experiments_valid e JOIN projects p ON p.id = e.project_id
  WHERE e.run_status = 'ok' AND e.primary_value IS NOT NULL
) r WHERE rank <= 50;

CREATE VIEW v_best AS
SELECT * FROM v_leaderboard WHERE rank = 1;

CREATE VIEW v_tried AS
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
            ELSE 'unscored' END AS summary_outcome
FROM g JOIN latest l USING (project_id, dedup_key);

CREATE VIEW v_negative AS
SELECT t.project_id, t.dedup_key, t.config, t.code_ref, t.attempts, t.seeds,
       t.n_no_change, t.n_worse, t.n_failed, t.best_value, t.last_id, t.last_notes,
       (SELECT array_agg(DISTINCT e.notes) FROM experiments_valid e
         WHERE e.project_id = t.project_id AND e.dedup_key = t.dedup_key AND e.notes IS NOT NULL) AS notes,
       t.last_at
FROM v_tried t
WHERE t.n_improved = 0 AND t.n_unscored = 0;

CREATE VIEW v_open_tasks AS
SELECT t.project_id, t.id, t.title, t.tags, t.priority, t.spec, t.attempts, t.max_attempts,
       t.status, t.lease_owner AS expired_owner, t.created_at
FROM tasks t
WHERE (t.status = 'open' OR (t.status = 'claimed' AND t.lease_expires_at < now()))
  AND t.attempts < t.max_attempts
  AND NOT EXISTS (SELECT 1 FROM tasks d WHERE d.id = ANY (t.depends_on) AND d.status <> 'done');

CREATE VIEW v_progress AS
SELECT e.project_id, e.id, e.author, e.created_at, e.primary_value, e.outcome,
       CASE WHEN p.metric_direction = 'min'
            THEN min(e.primary_value) OVER w ELSE max(e.primary_value) OVER w END AS best_so_far,
       (row_number() OVER w)::int AS n,
       sum(coalesce(e.tokens, 0)) OVER w AS tokens_cum
FROM experiments_valid e JOIN projects p ON p.id = e.project_id
WHERE e.run_status = 'ok' AND e.primary_value IS NOT NULL
WINDOW w AS (PARTITION BY e.project_id ORDER BY e.created_at, e.id);

CREATE VIEW v_agent_stats AS
SELECT a.project_id, a.id AS agent, a.role, a.model,
       (SELECT count(*) FROM experiments e WHERE e.project_id = a.project_id AND e.author = a.id)::int AS experiments,
       (SELECT count(*) FROM experiments_valid e WHERE e.project_id = a.project_id AND e.author = a.id
          AND e.outcome = 'improved')::int AS improved,
       (SELECT count(*) FROM claims c WHERE c.project_id = a.project_id AND c.author = a.id)::int AS claims,
       (SELECT count(*) FROM dedup_hits h WHERE h.project_id = a.project_id AND h.agent_id = a.id
          AND h.kind = 'avoided')::int AS dedup_avoided,
       (SELECT count(*) FROM dedup_hits h WHERE h.project_id = a.project_id AND h.agent_id = a.id
          AND h.kind = 'rejected')::int AS dedup_rejected,
       greatest(a.created_at,
         (SELECT max(created_at) FROM experiments e WHERE e.project_id = a.project_id AND e.author = a.id),
         (SELECT max(created_at) FROM claims c WHERE c.project_id = a.project_id AND c.author = a.id),
         (SELECT max(created_at) FROM task_events t WHERE t.project_id = a.project_id AND t.agent_id = a.id),
         (SELECT max(created_at) FROM brief_log b WHERE b.project_id = a.project_id AND b.agent_id = a.id))
         AS last_active_at
FROM agents a;

CREATE VIEW v_dedup_savings AS
SELECT p.id AS project_id,
       (SELECT count(*) FROM dedup_hits h WHERE h.project_id = p.id AND h.kind = 'rejected')::int AS rejected,
       (SELECT count(*) FROM dedup_hits h WHERE h.project_id = p.id AND h.kind = 'avoided')::int AS avoided
FROM projects p;

CREATE VIEW v_activity AS
SELECT project_id, 'experiment' AS kind, id::text AS id, author AS agent,
       run_status || ' ' || outcome || coalesce(' ' || primary_value::text, '') AS summary, created_at
FROM experiments
UNION ALL
SELECT project_id, 'claim', id::text, author, kind || ': ' || left(statement, 120), created_at FROM claims
UNION ALL
SELECT project_id, 'decision', id::text, decided_by,
       target_type || ' -> ' || new_status || ' (' || oracle || ')', created_at FROM decisions
UNION ALL
SELECT project_id, 'link', id::text, author, src_type || ' ' || relation || ' ' || dst_type, created_at FROM links
UNION ALL
SELECT project_id, 'source', id::text, author, left(coalesce(title, uri), 120), created_at FROM sources
UNION ALL
SELECT project_id, 'task_event', task_id::text, agent_id, event, created_at FROM task_events;
