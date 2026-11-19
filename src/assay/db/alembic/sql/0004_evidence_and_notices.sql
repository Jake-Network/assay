SET search_path = assay;

CREATE OR REPLACE FUNCTION notify_insert() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE channel text;
BEGIN
  -- Keep existing short channels. The colon cannot occur in a valid project slug.
  -- Match notification_channel in db/conn.py.
  channel := CASE WHEN octet_length(NEW.project_id) <= 57 THEN 'assay_' || NEW.project_id
                  ELSE 'assay_:' || md5(NEW.project_id) END;
  PERFORM pg_notify(channel, json_build_object('type', TG_TABLE_NAME, 'id', NEW.id, 'op', TG_OP)::text);
  RETURN NULL;
END $$;

CREATE OR REPLACE VIEW experiments_valid AS
SELECT e.* FROM experiments e
WHERE NOT EXISTS (SELECT 1 FROM decisions d
                  WHERE d.project_id = e.project_id AND d.target_type = 'experiment'
                    AND d.target_id = e.id AND d.new_status = 'invalidated');

CREATE OR REPLACE VIEW v_claim_status AS
WITH last_dec AS (
  SELECT DISTINCT ON (project_id, target_id) project_id, target_id, new_status, oracle, decided_by,
         created_at, evidence_ids
  FROM decisions WHERE target_type = 'claim'
  ORDER BY project_id, target_id, created_at DESC, id DESC
), inval AS (
  SELECT DISTINCT project_id, target_id AS id FROM decisions WHERE new_status = 'invalidated'
), ev AS (
  SELECT l.project_id, l.dst_id AS claim_id, l.relation,
         (l.src_type = 'experiment' AND i.id IS NOT NULL) AS invalid,
         coalesce(e.author, c.author, s.author, l.author) AS src_author
  FROM links l
  LEFT JOIN inval i ON i.project_id = l.project_id AND i.id = l.src_id
  LEFT JOIN experiments e ON l.src_type = 'experiment' AND e.project_id = l.project_id AND e.id = l.src_id
  LEFT JOIN claims c ON l.src_type = 'claim' AND c.project_id = l.project_id AND c.id = l.src_id
  LEFT JOIN sources s ON l.src_type = 'source' AND s.project_id = l.project_id AND s.id = l.src_id
  WHERE l.dst_type = 'claim' AND l.relation IN ('supports','refutes')
)
SELECT c.project_id, c.id, c.kind, c.statement, c.tags, c.author, c.task_id, c.created_at,
       coalesce(d.new_status, 'proposed') AS status,
       d.oracle AS last_oracle, d.decided_by AS last_decided_by, d.created_at AS decided_at,
       (SELECT count(*) FROM ev WHERE project_id = c.project_id AND claim_id = c.id
                               AND relation = 'supports' AND NOT invalid)::int AS n_support,
       (SELECT count(*) FROM ev WHERE project_id = c.project_id AND claim_id = c.id
                               AND relation = 'refutes' AND NOT invalid)::int AS n_refute,
       (SELECT count(DISTINCT src_author) FROM ev WHERE project_id = c.project_id AND claim_id = c.id
                                              AND relation = 'supports' AND NOT invalid)::int AS n_support_authors,
       CASE WHEN cardinality(d.evidence_ids) > 0 THEN
         NOT EXISTS (SELECT 1 FROM unnest(d.evidence_ids) AS evidence(id)
                     LEFT JOIN inval i ON i.project_id = c.project_id AND i.id = evidence.id WHERE i.id IS NULL)
       ELSE
         EXISTS (SELECT 1 FROM ev WHERE project_id = c.project_id AND claim_id = c.id
                                  AND relation = 'supports' AND invalid)
         AND NOT EXISTS (SELECT 1 FROM ev WHERE project_id = c.project_id AND claim_id = c.id
                                          AND relation = 'supports' AND NOT invalid)
       END AS evidence_invalidated
FROM claims c LEFT JOIN last_dec d ON d.project_id = c.project_id AND d.target_id = c.id;

CREATE OR REPLACE VIEW v_agent_stats AS
SELECT a.project_id, a.id AS agent, a.role, a.model,
       (SELECT count(*) FROM experiments_valid e
         WHERE e.project_id = a.project_id AND e.author = a.id)::int AS experiments,
       (SELECT count(*) FROM experiments_valid e WHERE e.project_id = a.project_id AND e.author = a.id
          AND e.outcome = 'improved')::int AS improved,
       (SELECT count(*) FROM claims c WHERE c.project_id = a.project_id AND c.author = a.id)::int AS claims,
       (SELECT count(*) FROM dedup_hits h WHERE h.project_id = a.project_id AND h.agent_id = a.id
          AND h.kind = 'avoided')::int AS dedup_avoided,
       (SELECT count(*) FROM dedup_hits h WHERE h.project_id = a.project_id AND h.agent_id = a.id
          AND h.kind = 'rejected')::int AS dedup_rejected,
       greatest(a.created_at,
         (SELECT max(created_at) FROM experiments_valid e WHERE e.project_id = a.project_id AND e.author = a.id),
         (SELECT max(created_at) FROM claims c WHERE c.project_id = a.project_id AND c.author = a.id),
         (SELECT max(created_at) FROM task_events t WHERE t.project_id = a.project_id AND t.agent_id = a.id),
         (SELECT max(created_at) FROM brief_log b WHERE b.project_id = a.project_id AND b.agent_id = a.id))
         AS last_active_at
FROM agents a;

CREATE OR REPLACE VIEW v_activity AS
SELECT project_id, 'experiment' AS kind, id::text AS id, author AS agent,
       run_status || ' ' || outcome || coalesce(' ' || primary_value::text, '') AS summary, created_at
FROM experiments_valid
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
