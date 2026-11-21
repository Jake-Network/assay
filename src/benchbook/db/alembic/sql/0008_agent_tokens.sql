-- Per-agent tokens and a login that can only append (benchbook.md §6 and §15: self-declared identity).
--
-- Who this is for. A session that can change agents.token_hash (the schema owner, a superuser) is trusted: it
-- could disable every trigger anyway, so local mode and the operator's connection behave as before. Every other
-- session (the benchbook_writer login) is bound to the agent whose token it presented:
--   * the token travels in the transaction-local setting benchbook.token. A setting any client can SET is only
--     safe to trust when it holds the credential itself, so the triggers never read a name from a setting: they
--     hash the token again and look the agent up. Setting a name, or a token hash, proves nothing.
--   * the setting is transaction-local, so a pooled connection carries no identity from one checkout to the next.
--   * author columns must name that agent; decisions, settings and task creation also need its role.
SET search_path = benchbook;

-- 1. The token: only its SHA-256 is stored ------------------------------------------------------------
ALTER TABLE agents ADD COLUMN token_hash text CHECK (token_hash ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX agents_token_hash ON agents (token_hash) WHERE token_hash IS NOT NULL;

-- 2. The writer login (cluster-wide, like benchbook_reader in 0003) -----------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'benchbook_writer') THEN
    -- LOGIN without a password: under password authentication nobody can connect as it until an operator
    -- runs ALTER ROLE benchbook_writer PASSWORD '...'. That statement is what turns team enforcement on.
    CREATE ROLE benchbook_writer LOGIN;
  END IF;
EXCEPTION
  WHEN duplicate_object OR unique_violation THEN
    NULL;  -- another database of this cluster created it at the same moment
  WHEN insufficient_privilege THEN
    -- a managed Postgres: the ledger's owner may not create roles. Nothing of this migration is kept (one
    -- transaction); after an administrator creates the role, migrate runs it again.
    RAISE EXCEPTION 'benchbook: % may not create the role benchbook_writer', current_user
      USING ERRCODE = 'insufficient_privilege',
            HINT = 'A database administrator runs:  CREATE ROLE benchbook_writer LOGIN;  Then run benchbook migrate again.';
END $$;

REVOKE ALL ON SCHEMA benchbook FROM benchbook_writer;
REVOKE ALL ON ALL TABLES IN SCHEMA benchbook FROM benchbook_writer;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA benchbook FROM benchbook_writer;
GRANT USAGE ON SCHEMA benchbook TO benchbook_writer;

-- Reads: every table and view, except that agents is granted by column so token_hash stays unreadable.
-- A later migration that adds a table or view grants it here the same way (tests/test_invariants.py checks).
DO $$
DECLARE r text;
BEGIN
  FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'benchbook' AND c.relkind IN ('r', 'v') AND c.relname <> 'agents' LOOP
    EXECUTE format('GRANT SELECT ON benchbook.%I TO benchbook_writer', r);
  END LOOP;
END $$;
GRANT SELECT (project_id, id, role, model, meta, created_at) ON agents TO benchbook_writer;

-- Writes: INSERT on the record tables, nothing on projects (rows) and agents. No UPDATE, DELETE or TRUNCATE on
-- any append-only table. The two mutable things keep a column-level UPDATE, checked by the triggers below:
-- the task queue (lease state) and projects.settings.
GRANT INSERT ON experiments, claims, sources, links, decisions, blobs, tasks, task_events, dedup_hits, brief_log,
                settings_events TO benchbook_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA benchbook TO benchbook_writer;
GRANT UPDATE (status, lease_owner, lease_expires_at, attempts, summary, spec, updated_at) ON tasks TO benchbook_writer;
GRANT UPDATE (settings) ON projects TO benchbook_writer;

-- 3. Token -> identity ------------------------------------------------------------------------------
-- Functions pin search_path (pg_catalog first, pg_temp last) so a caller's search_path cannot swap an operator
-- or a table under them. The existing trigger functions get the same pin: an untrusted login now fires them.
ALTER FUNCTION forbid_mutation() SET search_path = pg_catalog, benchbook, pg_temp;
ALTER FUNCTION check_decider_role() SET search_path = pg_catalog, benchbook, pg_temp;
ALTER FUNCTION notify_insert() SET search_path = pg_catalog, benchbook, pg_temp;
ALTER FUNCTION forbid_agent_change() SET search_path = pg_catalog, benchbook, pg_temp;

-- The agent this transaction's token belongs to in one project, or NULLs. SECURITY DEFINER because the
-- caller cannot read token_hash; it returns only what the caller's own token already proves.
CREATE FUNCTION session_identity(p_project text, OUT agent_id text, OUT agent_role text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, benchbook, pg_temp AS $$
DECLARE tok text := nullif(current_setting('benchbook.token', true), '');
BEGIN
  IF tok IS NULL THEN
    RETURN;
  END IF;
  SELECT a.id, a.role INTO agent_id, agent_role FROM benchbook.agents a
  WHERE a.project_id = p_project AND a.token_hash = encode(sha256(convert_to(tok, 'UTF8')), 'hex');
END $$;

-- Check a token and bind it to the current transaction. Clients call this with the token as a bind
-- parameter (never inlined in SQL text: pg_stat_activity shows statement text to sessions of the same role).
CREATE FUNCTION authenticate(p_project text, p_token text, OUT agent_id text, OUT agent_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, benchbook, pg_temp AS $$
BEGIN
  SELECT a.id, a.role INTO agent_id, agent_role FROM benchbook.agents a
  WHERE a.project_id = p_project AND a.token_hash = encode(sha256(convert_to(p_token, 'UTF8')), 'hex');
  IF agent_id IS NULL THEN
    RAISE EXCEPTION 'benchbook: the token is not valid for project %', p_project USING ERRCODE = 'invalid_password';
  END IF;
  -- is_local: the identity ends with the transaction. set_config keeps the token out of statement text; where
  -- this function's owner may not call it (0006 revoked it from PUBLIC under another owner), SET LOCAL does it.
  IF has_function_privilege('pg_catalog.set_config(text, text, boolean)', 'EXECUTE') THEN
    PERFORM set_config('benchbook.token', p_token, true);
  ELSE
    EXECUTE format('SET LOCAL benchbook.token = %L', p_token);
  END IF;
END $$;

REVOKE ALL ON FUNCTION session_identity(text), authenticate(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION session_identity(text), authenticate(text, text) TO benchbook_writer;

-- True for a session that could mint identities anyway (it may write token hashes): the owner, a superuser.
-- Runs as the caller, so it sees the role the statement really runs under.
CREATE FUNCTION session_trusted() RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, benchbook, pg_temp AS $$
  SELECT has_column_privilege('benchbook.agents', 'token_hash', 'UPDATE')
$$;

-- The identity an untrusted session must have before it writes; raises when there is none.
CREATE FUNCTION required_identity(p_project text, OUT agent_id text, OUT agent_role text)
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, benchbook, pg_temp AS $$
BEGIN
  SELECT s.agent_id, s.agent_role INTO agent_id, agent_role FROM benchbook.session_identity(p_project) s;
  IF agent_id IS NULL THEN
    RAISE EXCEPTION 'benchbook: this connection has not presented an agent token for project %; set BENCHBOOK_TOKEN (an operator issues it with benchbook agent add)', p_project
      USING ERRCODE = 'insufficient_privilege';
  END IF;
END $$;

-- 4. Author columns come from the identity, not from what the client sent -----------------------------
-- TG_ARGV[0]: the column that names the writer. TG_ARGV[1] = 'decider': the row is a lead-only action.
-- A row that leaves the column out gets the token's agent. A row naming anyone else is refused rather than
-- silently rewritten, so a forgery attempt is visible to the client that tried it.
CREATE FUNCTION bind_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, benchbook, pg_temp AS $$
DECLARE me record; named text;
BEGIN
  IF benchbook.session_trusted() THEN
    RETURN NEW;
  END IF;
  me := benchbook.required_identity(NEW.project_id);
  EXECUTE format('SELECT ($1).%I::text', TG_ARGV[0]) INTO named USING NEW;
  IF named IS NULL THEN
    NEW := jsonb_populate_record(NEW, jsonb_build_object(TG_ARGV[0], me.agent_id));
  ELSIF named <> me.agent_id THEN
    RAISE EXCEPTION 'benchbook: the token belongs to agent %; it cannot write % as %', me.agent_id, TG_TABLE_NAME,
      named USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF TG_NARGS > 1 AND TG_ARGV[1] = 'decider' AND me.agent_role NOT IN ('orchestrator', 'human') THEN
    RAISE EXCEPTION 'benchbook: agent % (role %) may not write %; only orchestrator or human (P2)', me.agent_id,
      me.agent_role, TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['experiments', 'claims', 'sources', 'links', 'blobs'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON benchbook.%I
                    FOR EACH ROW EXECUTE FUNCTION benchbook.bind_identity(''author'')', t || '_identity', t);
  END LOOP;
  FOREACH t IN ARRAY ARRAY['dedup_hits', 'brief_log'] LOOP
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT ON benchbook.%I
                    FOR EACH ROW EXECUTE FUNCTION benchbook.bind_identity(''agent_id'')', t || '_identity', t);
  END LOOP;
END $$;
-- decisions_identity fires before decisions_role (alphabetical), which checks the role of that same name.
CREATE TRIGGER decisions_identity BEFORE INSERT ON decisions
  FOR EACH ROW EXECUTE FUNCTION bind_identity('decided_by');
CREATE TRIGGER settings_events_identity BEFORE INSERT ON settings_events
  FOR EACH ROW EXECUTE FUNCTION bind_identity('agent_id', 'decider');

-- Settings are a lead-only action; the column grant already limits the writer to the settings column.
CREATE FUNCTION guard_project() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, benchbook, pg_temp AS $$
DECLARE me record;
BEGIN
  IF benchbook.session_trusted() THEN
    RETURN NEW;
  END IF;
  me := benchbook.required_identity(OLD.id);
  IF me.agent_role NOT IN ('orchestrator', 'human') THEN
    RAISE EXCEPTION 'benchbook: agent % (role %) may not change project settings; only orchestrator or human (P2)',
      me.agent_id, me.agent_role USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF to_jsonb(NEW) - 'settings' IS DISTINCT FROM to_jsonb(OLD) - 'settings' THEN
    RAISE EXCEPTION 'benchbook: only the settings of project % may change', OLD.id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER projects_identity BEFORE UPDATE ON projects
  FOR EACH ROW EXECUTE FUNCTION guard_project();

-- The task queue is the one table agents update. An untrusted session may make exactly the changes the SDK
-- makes, each tied to its identity; anything else is refused.
CREATE FUNCTION guard_task() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, benchbook, pg_temp AS $$
DECLARE me record; decider boolean; same_work boolean;
BEGIN
  IF benchbook.session_trusted() THEN
    RETURN NEW;
  END IF;
  me := benchbook.required_identity(NEW.project_id);
  decider := me.agent_role IN ('orchestrator', 'human');
  IF TG_OP = 'INSERT' THEN
    IF NEW.created_by IS DISTINCT FROM me.agent_id THEN
      RAISE EXCEPTION 'benchbook: the token belongs to agent %; it cannot create a task as %', me.agent_id,
        NEW.created_by USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- create_tasks: an open, unowned task, from the lead only
    IF decider AND NEW.status = 'open' AND NEW.lease_owner IS NULL AND NEW.attempts = 0 THEN
      RETURN NEW;
    END IF;
    -- create_intent: a task its creator already holds
    IF NEW.status = 'claimed' AND NEW.lease_owner = me.agent_id AND 'intent' = ANY (NEW.tags)
       AND NEW.lease_expires_at IS NOT NULL THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'benchbook: agent % (role %) may not create this task; only orchestrator or human create open tasks, and an intent is held by its creator (P2)',
      me.agent_id, me.agent_role USING ERRCODE = 'insufficient_privilege';
  END IF;

  IF (NEW.id, NEW.project_id, NEW.title, NEW.tags, NEW.priority, NEW.depends_on, NEW.created_by, NEW.created_at,
      NEW.max_attempts)
     IS DISTINCT FROM (OLD.id, OLD.project_id, OLD.title, OLD.tags, OLD.priority, OLD.depends_on, OLD.created_by,
                       OLD.created_at, OLD.max_attempts) THEN
    RAISE EXCEPTION 'benchbook: task % keeps its title, tags, priority, dependencies and creator', OLD.id
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  same_work := NEW.spec = OLD.spec AND NEW.summary IS NOT DISTINCT FROM OLD.summary;
  -- the holder: heartbeat, update_intent (only an intent's spec changes), complete_task
  IF OLD.status = 'claimed' AND OLD.lease_owner = me.agent_id AND NEW.lease_owner = me.agent_id
     AND NEW.attempts = OLD.attempts THEN
    IF NEW.status = 'claimed' AND NEW.lease_expires_at IS NOT NULL
       AND NEW.summary IS NOT DISTINCT FROM OLD.summary AND (NEW.spec = OLD.spec OR 'intent' = ANY (OLD.tags)) THEN
      RETURN NEW;
    END IF;
    IF NEW.status IN ('done', 'failed') AND NEW.spec = OLD.spec THEN
      RETURN NEW;
    END IF;
  END IF;
  -- next_task: claim an open task, or one whose lease ran out, in your own name
  IF NEW.status = 'claimed' AND NEW.lease_owner = me.agent_id AND NEW.lease_expires_at IS NOT NULL
     AND NEW.attempts = OLD.attempts + 1 AND OLD.attempts < OLD.max_attempts AND same_work
     AND (OLD.status = 'open' OR (OLD.status = 'claimed' AND OLD.lease_expires_at < clock_timestamp())) THEN
    RETURN NEW;
  END IF;
  -- next_task: a lease that ran out with no attempts left becomes failed, whoever notices
  IF NEW.status = 'failed' AND OLD.status = 'claimed' AND OLD.lease_expires_at < clock_timestamp()
     AND OLD.attempts >= OLD.max_attempts AND NEW.attempts = OLD.attempts AND same_work
     AND NEW.lease_owner IS NOT DISTINCT FROM OLD.lease_owner
     AND NEW.lease_expires_at IS NOT DISTINCT FROM OLD.lease_expires_at THEN
    RETURN NEW;
  END IF;
  -- cancel_task: the lead only
  IF decider AND NEW.status = 'cancelled' AND OLD.status IN ('open', 'claimed') AND NEW.attempts = OLD.attempts
     AND same_work AND NEW.lease_owner IS NOT DISTINCT FROM OLD.lease_owner THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'benchbook: agent % (role %) may not change task % this way (status % held by % -> % held by %)',
    me.agent_id, me.agent_role, OLD.id, OLD.status, coalesce(OLD.lease_owner, 'nobody'), NEW.status,
    coalesce(NEW.lease_owner, 'nobody') USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER tasks_identity BEFORE INSERT OR UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION guard_task();

-- Task events name the acting agent, with one exception the SDK needs: whoever notices that a lease ran out
-- writes 'expired' (and 'failed' when no attempts are left) in the name of the agent that held it.
CREATE FUNCTION guard_task_event() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, benchbook, pg_temp AS $$
DECLARE me record; t record;
BEGIN
  IF benchbook.session_trusted() THEN
    RETURN NEW;
  END IF;
  me := benchbook.required_identity(NEW.project_id);
  IF NEW.agent_id IS NOT DISTINCT FROM me.agent_id THEN
    RETURN NEW;
  END IF;
  SELECT status, lease_owner, lease_expires_at INTO t FROM benchbook.tasks
  WHERE id = NEW.task_id AND project_id = NEW.project_id;
  IF FOUND AND NEW.agent_id IS NOT NULL THEN
    -- the caller has just taken over the expired lease
    IF NEW.event = 'expired' AND t.status = 'claimed' AND t.lease_owner = me.agent_id THEN
      RETURN NEW;
    END IF;
    -- the lease ran out with no attempts left: the task failed and still names its last holder
    IF NEW.event IN ('expired', 'failed') AND t.status = 'failed' AND t.lease_owner = NEW.agent_id
       AND t.lease_expires_at IS NOT NULL THEN
      RETURN NEW;
    END IF;
  END IF;
  RAISE EXCEPTION 'benchbook: the token belongs to agent %; it cannot write a task event (%) as %', me.agent_id,
    NEW.event, coalesce(NEW.agent_id, '(nobody)') USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER task_events_identity BEFORE INSERT ON task_events
  FOR EACH ROW EXECUTE FUNCTION guard_task_event();
