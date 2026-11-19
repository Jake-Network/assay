-- assay core schema (spec §5). Append-only knowledge tables, enforced by triggers.
CREATE SCHEMA IF NOT EXISTS assay;
SET search_path = assay;

CREATE TABLE projects (
  id                text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{0,62}$'),
  title             text NOT NULL,
  goal              text NOT NULL,
  primary_metric    text,
  metric_direction  text CHECK (metric_direction IN ('max','min')),
  improve_epsilon   double precision NOT NULL DEFAULT 0,
  pack              text NOT NULL DEFAULT 'core',
  settings          jsonb NOT NULL DEFAULT '{}',
  created_at        timestamptz NOT NULL DEFAULT now(),
  CHECK ((primary_metric IS NULL) = (metric_direction IS NULL))
);

CREATE TABLE agents (
  project_id  text NOT NULL REFERENCES projects(id),
  id          text NOT NULL,
  role        text NOT NULL CHECK (role IN ('worker','orchestrator','human','harness')),
  model       text,
  meta        jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, id)
);

CREATE TABLE tasks (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id        text NOT NULL REFERENCES projects(id),
  title             text NOT NULL,
  spec              jsonb NOT NULL DEFAULT '{}',
  tags              text[] NOT NULL DEFAULT '{}',
  priority          int NOT NULL DEFAULT 0,
  depends_on        uuid[] NOT NULL DEFAULT '{}',
  created_by        text NOT NULL,
  status            text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','claimed','done','failed','cancelled')),
  lease_owner       text,
  lease_expires_at  timestamptz,
  attempts          int NOT NULL DEFAULT 0,
  max_attempts      int NOT NULL DEFAULT 3,
  summary           text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON tasks (project_id, status, priority DESC, created_at);

CREATE TABLE task_events (
  id          bigserial PRIMARY KEY,
  project_id  text NOT NULL REFERENCES projects(id),
  task_id     uuid NOT NULL REFERENCES tasks(id),
  event       text NOT NULL,
  agent_id    text,
  detail      jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON task_events (task_id, id);

CREATE TABLE experiments (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id     text NOT NULL REFERENCES projects(id),
  author         text NOT NULL,
  task_id        uuid REFERENCES tasks(id),
  config         jsonb NOT NULL,
  code_ref       text,
  dedup_key      text NOT NULL,
  seed           bigint,
  run_status     text NOT NULL CHECK (run_status IN ('ok','error','timeout','invalid')),
  metrics        jsonb NOT NULL DEFAULT '{}',
  primary_value  double precision,
  outcome        text NOT NULL
                 CHECK (outcome IN ('improved','no_change','worse','failed','unscored')),
  notes          text CHECK (length(notes) <= 2000),
  artifacts      jsonb NOT NULL DEFAULT '[]',
  trace_ref      jsonb,
  duration_ms    bigint,
  tokens         int,
  cost_usd       numeric(12,6),
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON experiments (project_id, dedup_key);
CREATE INDEX ON experiments (project_id, created_at DESC);
CREATE UNIQUE INDEX experiments_no_exact_dup
  ON experiments (project_id, dedup_key, coalesce(seed, -1))
  WHERE run_status = 'ok';

CREATE TABLE claims (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id      text NOT NULL REFERENCES projects(id),
  kind            text NOT NULL,
  statement       text NOT NULL CHECK (length(statement) BETWEEN 1 AND 500),
  statement_hash  text NOT NULL,
  payload         jsonb NOT NULL DEFAULT '{}',
  tags            text[] NOT NULL DEFAULT '{}',
  author          text NOT NULL,
  task_id         uuid REFERENCES tasks(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, statement_hash)
);

CREATE TABLE sources (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  text NOT NULL REFERENCES projects(id),
  uri         text NOT NULL,
  title       text,
  locator     text,
  sha256      text,
  author      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE links (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  text NOT NULL REFERENCES projects(id),
  src_type    text NOT NULL CHECK (src_type IN ('claim','experiment','source','task')),
  src_id      uuid NOT NULL,
  relation    text NOT NULL CHECK (relation IN
              ('supports','refutes','derived_from','depends_on','supersedes','replicates','cites')),
  dst_type    text NOT NULL CHECK (dst_type IN ('claim','experiment','source','task')),
  dst_id      uuid NOT NULL,
  note        text CHECK (length(note) <= 500),
  author      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, src_id, relation, dst_id)
);
CREATE INDEX ON links (dst_id);

CREATE TABLE decisions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id     text NOT NULL REFERENCES projects(id),
  target_type    text NOT NULL CHECK (target_type IN ('claim','experiment')),
  target_id      uuid NOT NULL,
  new_status     text NOT NULL CHECK (new_status IN
                 ('supported','refuted','inconclusive','verified','superseded','retracted',
                  'invalidated')),
  CHECK ((target_type = 'experiment') = (new_status = 'invalidated')),
  oracle         text NOT NULL CHECK (oracle IN
                 ('metric','rerun','pytest','lean','human','llm_judge','none')),
  oracle_detail  jsonb NOT NULL DEFAULT '{}',
  rationale      text NOT NULL CHECK (length(rationale) BETWEEN 1 AND 1000),
  evidence_ids   uuid[] NOT NULL DEFAULT '{}',
  decided_by     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (new_status <> 'verified' OR oracle IN ('metric','rerun','pytest','lean','human')),
  CHECK (new_status <> 'verified' OR cardinality(evidence_ids) > 0)
);
CREATE INDEX ON decisions (target_id, created_at DESC);

CREATE TABLE brief_log (
  id          bigserial PRIMARY KEY,
  project_id  text NOT NULL REFERENCES projects(id),
  agent_id    text NOT NULL,
  task_id     uuid,
  tokens_est  int NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON brief_log (project_id, agent_id, id DESC);

CREATE TABLE dedup_hits (
  id                bigserial PRIMARY KEY,
  project_id        text NOT NULL REFERENCES projects(id),
  kind              text NOT NULL CHECK (kind IN ('rejected','avoided')),
  dedup_key         text NOT NULL,
  existing_id       uuid,
  agent_id          text,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- Triggers --------------------------------------------------------------

CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'assay: % on %.% is forbidden (append-only, P1); write a new row and link it with supersedes',
    TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
    USING ERRCODE = 'restrict_violation';
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['experiments','claims','sources','links','decisions',
                           'task_events','dedup_hits','brief_log'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON assay.%I
                    FOR EACH ROW EXECUTE FUNCTION assay.forbid_mutation()', t || '_append_only', t);
  END LOOP;
END $$;

CREATE FUNCTION check_decider_role() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r text;
BEGIN
  SELECT role INTO r FROM assay.agents WHERE project_id = NEW.project_id AND id = NEW.decided_by;
  IF r IS NULL OR r NOT IN ('orchestrator','human') THEN
    RAISE EXCEPTION 'assay: agent % (role %) may not write decisions; only orchestrator or human (P2)',
      NEW.decided_by, coalesce(r, 'unregistered')
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER decisions_role BEFORE INSERT ON decisions
  FOR EACH ROW EXECUTE FUNCTION check_decider_role();

CREATE FUNCTION notify_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('assay_' || NEW.project_id,
                    json_build_object('type', TG_TABLE_NAME, 'id', NEW.id, 'op', TG_OP)::text);
  RETURN NULL;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['experiments','claims','sources','links','decisions',
                           'task_events','dedup_hits','brief_log'] LOOP
    EXECUTE format('CREATE TRIGGER %I AFTER INSERT ON assay.%I
                    FOR EACH ROW EXECUTE FUNCTION assay.notify_insert()', t || '_notify', t);
  END LOOP;
END $$;
CREATE TRIGGER tasks_notify AFTER INSERT OR UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION notify_insert();
