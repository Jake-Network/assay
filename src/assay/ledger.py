"""All business logic lives here (P10). CLI, MCP server, web viewer and hooks are thin adapters."""
from __future__ import annotations

import atexit
import json
import math
import re
import threading
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from jsonschema import Draft202012Validator
from psycopg import sql as psql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import config as localcfg
from .db import conn as dbconn
from .db.migrate import head as migrate_head
from .db.migrate import migrate as run_migrate
from .dedup import canonical_json, dedup_key, statement_hash
from .errors import (
    Ambiguous,
    AssayError,
    InvalidTransition,
    LeaseLost,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from .ids import KIND_BY_PREFIX, TABLE, short
from .models import (
    ClaimResult,
    Decision,
    ExperimentSummary,
    Project,
    RecordResult,
    Task,
    TriedResult,
)
from .packs import Pack, check, depth, load_pack
from .tokens import estimate_tokens

ROLES = ("worker", "orchestrator", "human", "harness")
DECIDERS = ("orchestrator", "human")
RUN_STATUSES = ("ok", "error", "timeout", "invalid")
RELATIONS = ("supports", "refutes", "derived_from", "depends_on", "supersedes", "replicates", "cites")
ORACLES = ("metric", "rerun", "pytest", "lean", "human", "llm_judge", "none")
NON_LLM_ORACLES = ("metric", "rerun", "pytest", "lean", "human")
CLAIM_STATUSES = ("supported", "refuted", "inconclusive", "verified", "superseded", "retracted")
# §6.1: transitions the SDK accepts without force=True
TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"supported", "refuted", "inconclusive", "superseded", "retracted"},
    "inconclusive": {"supported", "refuted", "superseded", "retracted"},
    "supported": {"verified", "refuted", "inconclusive", "superseded", "retracted"},
    "verified": {"superseded", "retracted"},
    "refuted": set(),
    "superseded": set(),
    "retracted": set(),
}
SQL_ROW_LIMIT = 200
SQL_TOKEN_LIMIT = 4000
_FORBIDDEN_SQL = re.compile(
    r"\b(set_config|set\s+role|reset|set\s+session|pg_read_\w+|pg_ls_dir|lo_\w+|dblink\w*|copy|"
    r"pg_terminate_backend|pg_cancel_backend)\b",
    re.IGNORECASE,
)
_ARTIFACTS = Draft202012Validator({
    "type": "array",
    "items": {"type": "object", "required": ["uri"],
              "properties": {"uri": {"type": "string", "minLength": 1}, "kind": {"type": "string"},
                             "sha256": {"type": "string"}, "bytes": {"type": "integer"}}},
})
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHORT_RE = re.compile(r"^(?:([ectsdl]):)?([0-9a-f-]{4,36})$")


def jsonable(v: Any) -> Any:
    if isinstance(v, (UUID, Decimal)):
        return str(v) if isinstance(v, UUID) else float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return v


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


class Ledger:
    """A handle on one project's ledger, acting as one agent."""

    _pools: dict[str, ConnectionPool[Any]] = {}
    _pools_lock = threading.Lock()
    _migrated: set[str] = set()

    def __init__(self, url: str, project: Project, agent: str, role: str):
        self.url = url
        self.project = project
        self.project_id = project.id
        self.agent = agent
        self.role = role
        self.pack: Pack = load_pack(project.pack)
        cs = project.settings.get("config_schema")
        self._config_validator = Draft202012Validator(cs) if cs else None

    # ------------------------------------------------------------------ setup

    @classmethod
    def _pool(cls, url: str) -> ConnectionPool[Any]:
        with cls._pools_lock:
            pool = cls._pools.get(url)
            if pool is None:
                pool = ConnectionPool(url, min_size=1, max_size=8, open=True, num_workers=1,
                                      kwargs={"row_factory": dict_row, "autocommit": False})
                if not cls._pools:
                    atexit.register(cls.close_all)
                cls._pools[url] = pool
            return pool

    @classmethod
    def close_all(cls) -> None:
        with cls._pools_lock:
            for pool in cls._pools.values():
                pool.close(timeout=0.5)
            cls._pools.clear()

    @classmethod
    def ensure_schema(cls, database_url: str | None = None) -> str:
        url = dbconn.database_url(database_url)
        if url not in cls._migrated:
            run_migrate(url)
            cls._migrated.add(url)
        return url

    @classmethod
    def open(cls, project: str | None = None, agent: str | None = None, role: str | None = None,
             database_url: str | None = None) -> Ledger:
        """Open a project's ledger. Project comes from the argument, ASSAY_PROJECT or .assay/config.toml.
        Agent defaults to ASSAY_AGENT, else 'human'. An unknown agent is registered with `role`
        (default worker; 'human' defaults to the human role)."""
        import os

        project = project or localcfg.load().project
        if not project:
            raise AssayError("no project: pass project=, set ASSAY_PROJECT, or run `assay init`")
        agent = agent or os.environ.get("ASSAY_AGENT") or "human"
        url = cls.ensure_schema(database_url)
        with cls._pool(url).connection() as conn:
            row = conn.execute("SELECT * FROM assay.projects WHERE id = %s", (project,)).fetchone()
            if row is None:
                raise NotFound(f"project {project!r} does not exist; run `assay init --project {project}`")
            p = Project(**row)
            actual = cls._ensure_agent(conn, p.id, agent, role)
        return cls(url, p, agent, actual)

    @staticmethod
    def _ensure_agent(conn: psycopg.Connection[Any], project_id: str, agent: str, role: str | None,
                      model: str | None = None) -> str:
        if not agent or len(agent) > 100:
            raise AssayError("agent name must be 1-100 characters")
        want = role or ("human" if agent == "human" else "worker")
        if want not in ROLES:
            raise AssayError(f"role must be one of {ROLES}")
        conn.execute(
            "INSERT INTO assay.agents(project_id, id, role, model) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (project_id, agent, want, model),
        )
        r = conn.execute("SELECT role FROM assay.agents WHERE project_id=%s AND id=%s",
                         (project_id, agent)).fetchone()
        assert r is not None
        return str(r["role"])

    @classmethod
    def init_project(cls, id: str, title: str | None = None, goal: str = "", metric: str | None = None,
                     direction: str | None = None, epsilon: float = 0.0,
                     config_schema: dict[str, Any] | None = None, settings: dict[str, Any] | None = None,
                     database_url: str | None = None) -> tuple[Project, bool]:
        """Create a project (idempotent). Registers lead (orchestrator), human (human), hook (harness).
        Returns (project, created)."""
        if (metric is None) != (direction is None):
            raise AssayError("--metric and --direction go together")
        if direction not in (None, "max", "min"):
            raise AssayError("direction must be max or min")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", id):
            raise AssayError("project id must be a lowercase slug: [a-z0-9][a-z0-9_-]*")
        st = dict(settings or {})
        if config_schema is not None:
            try:
                Draft202012Validator.check_schema(config_schema)
            except Exception as e:  # jsonschema.SchemaError
                raise ValidationFailed([f"config_schema: {e}"]) from e
            st["config_schema"] = config_schema
        url = cls.ensure_schema(database_url)
        with cls._pool(url).connection() as conn:
            row = conn.execute(
                "INSERT INTO assay.projects(id,title,goal,primary_metric,metric_direction,improve_epsilon,settings)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING RETURNING *",
                (id, title or id, goal or "(no goal set)", metric, direction, epsilon, Jsonb(st)),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute("SELECT * FROM assay.projects WHERE id=%s", (id,)).fetchone()
            for a, r in (("lead", "orchestrator"), ("human", "human"), ("hook", "harness")):
                cls._ensure_agent(conn, id, a, r)
        return Project(**row), created

    def as_agent(self, agent: str, role: str | None = None) -> Ledger:
        with self._conn() as conn:
            actual = self._ensure_agent(conn, self.project_id, agent, role)
        return Ledger(self.url, self.project, agent, actual)

    def _conn(self) -> Any:
        return self._pool(self.url).connection()

    def _require_decider(self) -> None:
        if self.role not in DECIDERS:
            raise PermissionDenied(f"{self.agent} has role {self.role}; only orchestrator or human may do this (P2)")

    # ------------------------------------------------------------------ ids

    def resolve(self, ref: str | UUID, kinds: Sequence[str] | None = None) -> tuple[str, UUID]:
        """Resolve 'e:91c0a2f1', a bare prefix or a full uuid to (kind, uuid)."""
        if isinstance(ref, UUID):
            ref = str(ref)
        s = ref.strip().lower()
        m = _SHORT_RE.match(s)
        if not m:
            raise NotFound(f"not an assay id: {ref!r} (expected e:/c:/t:/s:/d:/l: + hex, or a uuid)")
        pfx, body = m.groups()
        search = [KIND_BY_PREFIX[pfx]] if pfx else list(kinds or TABLE.keys())
        if kinds is not None:
            search = [k for k in search if k in kinds]
            if not search:
                raise NotFound(f"{ref!r} is not one of: {', '.join(kinds)}")
        hits: list[tuple[str, UUID]] = []
        with self._conn() as conn:
            for kind in search:
                q = psql.SQL("SELECT id FROM assay.{} WHERE project_id = %s AND ").format(psql.Identifier(TABLE[kind]))
                if _UUID_RE.match(body):
                    rows = conn.execute(q + psql.SQL("id = %s"), (self.project_id, body)).fetchall()
                else:
                    rows = conn.execute(q + psql.SQL("id::text LIKE %s LIMIT 5"),
                                        (self.project_id, body + "%")).fetchall()
                hits += [(kind, r["id"]) for r in rows]
        if not hits:
            raise NotFound(f"no record {ref!r} in project {self.project_id}")
        if len(hits) > 1:
            raise Ambiguous(f"{ref!r} is ambiguous: " + ", ".join(short(k, i) for k, i in hits[:5]))
        return hits[0]

    def _rid(self, ref: str | UUID | None, kind: str) -> UUID | None:
        return None if ref is None else self.resolve(ref, [kind])[1]

    # ------------------------------------------------------------------ tasks

    def create_tasks(self, tasks: Sequence[dict[str, Any]]) -> list[UUID]:
        self._require_decider()
        problems: list[str] = []
        for i, t in enumerate(tasks):
            if not isinstance(t.get("title"), str) or not t["title"].strip():
                problems.append(f"tasks[{i}].title: required non-empty string")
            if not isinstance(t.get("spec", {}), dict):
                problems.append(f"tasks[{i}].spec: must be an object")
            tags = t.get("tags", [])
            if not isinstance(tags, list) or not all(isinstance(x, str) for x in tags):
                problems.append(f"tasks[{i}].tags: must be a list of strings")
        if problems:
            raise ValidationFailed(problems)
        ids: list[UUID] = []
        resolved_deps = [[self._rid(d, "task") for d in t.get("depends_on", [])] for t in tasks]
        with self._conn() as conn:
            for t, deps in zip(tasks, resolved_deps, strict=True):
                r = conn.execute(
                    "INSERT INTO assay.tasks(project_id,title,spec,tags,priority,depends_on,created_by,max_attempts)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (self.project_id, t["title"].strip(), Jsonb(t.get("spec", {})), list(t.get("tags", [])),
                     int(t.get("priority", 0)), deps, self.agent, int(t.get("max_attempts", 3))),
                ).fetchone()
                ids.append(r["id"])
                self._event(conn, r["id"], "created", {})
        return ids

    def _event(self, conn: psycopg.Connection[Any], task_id: UUID, event: str, detail: dict[str, Any],
               agent: str | None = None) -> None:
        conn.execute(
            "INSERT INTO assay.task_events(project_id,task_id,event,agent_id,detail) VALUES (%s,%s,%s,%s,%s)",
            (self.project_id, task_id, event, agent if agent is not None else self.agent, Jsonb(detail)),
        )

    def next_task(self, tags: Sequence[str] | None = None, lease_seconds: int = 900) -> Task | None:
        """Atomically claim the highest-priority claimable task (spec §10). None if nothing is claimable."""
        if lease_seconds <= 0:
            raise AssayError("lease_seconds must be positive")
        with self._conn() as conn:
            # Exhausted tasks whose lease ran out become failed here; there is no scheduler (P8).
            conn.execute(
                """WITH f AS (
                     UPDATE assay.tasks SET status='failed', updated_at=now()
                     WHERE project_id=%(p)s AND status='claimed' AND lease_expires_at < now()
                       AND attempts >= max_attempts
                     RETURNING id, lease_owner)
                   INSERT INTO assay.task_events(project_id, task_id, event, agent_id, detail)
                   SELECT %(p)s, id, e.event, lease_owner, '{"reason":"lease expired, attempts exhausted"}'
                   FROM f CROSS JOIN (VALUES ('expired'), ('failed')) e(event)""",
                {"p": self.project_id},
            )
            row = conn.execute(
                """WITH c AS (
                     SELECT t.id, t.status AS prev_status, t.lease_owner AS prev_owner FROM assay.tasks t
                     WHERE t.project_id = %(p)s
                       AND (t.status = 'open' OR (t.status = 'claimed' AND t.lease_expires_at < now()))
                       AND t.attempts < t.max_attempts
                       AND (cardinality(%(tags)s::text[]) = 0 OR t.tags && %(tags)s::text[])
                       AND NOT EXISTS (SELECT 1 FROM assay.tasks d
                                       WHERE d.id = ANY (t.depends_on) AND d.status <> 'done')
                     ORDER BY t.priority DESC, t.created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1)
                   UPDATE assay.tasks t
                   SET status = 'claimed', lease_owner = %(me)s,
                       lease_expires_at = now() + make_interval(secs => %(secs)s),
                       attempts = t.attempts + 1, updated_at = now()
                   FROM c WHERE t.id = c.id
                   RETURNING t.*, c.prev_status, c.prev_owner""",
                {"p": self.project_id, "tags": list(tags or []), "me": self.agent, "secs": lease_seconds},
            ).fetchone()
            if row is None:
                return None
            prev_status, prev_owner = row.pop("prev_status"), row.pop("prev_owner")
            if prev_status == "claimed":
                self._event(conn, row["id"], "expired", {"reason": "lease expired"}, agent=prev_owner)
            self._event(conn, row["id"], "claimed", {"lease_seconds": lease_seconds, "attempt": row["attempts"]})
            return Task(**row)

    def heartbeat(self, task_id: str | UUID, lease_seconds: int = 900) -> datetime:
        tid = self._rid(task_id, "task")
        with self._conn() as conn:
            r = conn.execute(
                "UPDATE assay.tasks SET lease_expires_at = now() + make_interval(secs => %s), updated_at=now()"
                " WHERE id=%s AND status='claimed' AND lease_owner=%s RETURNING lease_expires_at",
                (lease_seconds, tid, self.agent),
            ).fetchone()
            if r is None:
                raise LeaseLost(short("task", tid))  # type: ignore[arg-type]
            self._event(conn, tid, "heartbeat", {})  # type: ignore[arg-type]
            return r["lease_expires_at"]  # type: ignore[no-any-return]

    def complete_task(self, task_id: str | UUID, status: str = "done", summary: str = "") -> Task:
        if status not in ("done", "failed"):
            raise AssayError("status must be done or failed")
        if len(summary) > 1000:
            raise ValidationFailed(["summary: at most 1000 characters (2-3 sentences); details belong in records"])
        tid = self._rid(task_id, "task")
        with self._conn() as conn:
            r = conn.execute(
                "UPDATE assay.tasks SET status=%s, summary=%s, lease_expires_at=NULL, updated_at=now()"
                " WHERE id=%s AND status='claimed' AND lease_owner=%s RETURNING *",
                (status, summary or None, tid, self.agent),
            ).fetchone()
            if r is None:
                raise LeaseLost(short("task", tid))  # type: ignore[arg-type]
            self._event(conn, tid, status, {"summary": summary})  # type: ignore[arg-type]
            return Task(**r)

    def cancel_task(self, task_id: str | UUID, reason: str) -> Task:
        self._require_decider()
        tid = self._rid(task_id, "task")
        with self._conn() as conn:
            r = conn.execute(
                "UPDATE assay.tasks SET status='cancelled', lease_expires_at=NULL, updated_at=now()"
                " WHERE id=%s AND status IN ('open','claimed') RETURNING *", (tid,),
            ).fetchone()
            if r is None:
                raise AssayError(f"{short('task', tid)} is not open or claimed")  # type: ignore[arg-type]
            self._event(conn, tid, "cancelled", {"reason": reason})  # type: ignore[arg-type]
            return Task(**r)

    def get_task(self, task_id: str | UUID) -> Task:
        tid = self._rid(task_id, "task")
        with self._conn() as conn:
            return Task(**conn.execute("SELECT * FROM assay.tasks WHERE id=%s", (tid,)).fetchone())

    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM assay.tasks WHERE project_id=%s AND (%s::text IS NULL OR status=%s)"
                " ORDER BY created_at LIMIT %s", (self.project_id, status, status, limit)).fetchall()
        return [Task(**r) for r in rows]

    # ------------------------------------------------------------------ experiments

    def _validate_config(self, cfg: Any) -> dict[str, Any]:
        if not isinstance(cfg, dict):
            raise ValidationFailed(["config: must be a JSON object"])
        canonical_json(cfg)  # validates JSON values, including ignored keys
        try:
            encoded = json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as e:
            raise ValidationFailed([f"config: {e}"]) from e
        stored: dict[str, Any] = json.loads(encoded)
        self.pack.validate("experiment.config", stored, "config")
        problems = []
        config_depth = depth(stored)
        if config_depth > self.pack.config_max_depth:
            problems.append(f"config: nesting depth {config_depth} exceeds {self.pack.config_max_depth}")
        size = len(encoded.encode("utf-8"))
        if size > self.pack.config_max_bytes:
            problems.append(f"config: {size} bytes serialised, limit {self.pack.config_max_bytes}")
        if problems:
            raise ValidationFailed(problems)
        if self._config_validator is not None:
            check(self._config_validator, stored, "config")
        return stored

    def _key(self, cfg: dict[str, Any], code_ref: str | None) -> str:
        return dedup_key(cfg, code_ref, self.pack.dedup_ignore)

    def check_tried(self, config: dict[str, Any], code_ref: str | None = None) -> TriedResult:
        """Call before running an evaluation. Exact matches mean: pick a different config (or a new seed
        when replicating on purpose)."""
        cfg = self._validate_config(config)
        key = self._key(cfg, code_ref)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, author, seed, run_status, outcome, primary_value, notes, created_at"
                " FROM assay.experiments_valid WHERE project_id=%s AND dedup_key=%s"
                " ORDER BY created_at DESC LIMIT 20", (self.project_id, key)).fetchall()
            agg = conn.execute(
                "SELECT count(DISTINCT seed) FILTER (WHERE run_status='ok') AS reps, best_value"
                " FROM assay.experiments_valid e LEFT JOIN assay.v_tried t USING (project_id, dedup_key)"
                " WHERE e.project_id=%s AND e.dedup_key=%s GROUP BY best_value",
                (self.project_id, key)).fetchone()
            if rows:
                conn.execute(
                    "INSERT INTO assay.dedup_hits(project_id,kind,dedup_key,existing_id,agent_id)"
                    " VALUES (%s,'avoided',%s,%s,%s)", (self.project_id, key, rows[0]["id"], self.agent))
        return TriedResult(dedup_key=key, exact=[ExperimentSummary(**r) for r in rows],
                           replicates=int(agg["reps"]) if agg else 0,
                           best_value=agg["best_value"] if agg else None)

    def record_experiment(self, config: dict[str, Any], metrics: dict[str, Any] | None = None,
                          run_status: str = "ok", seed: int | None = None, code_ref: str | None = None,
                          notes: str | None = None, artifacts: list[dict[str, Any]] | None = None,
                          task_id: str | UUID | None = None, trace_ref: dict[str, Any] | None = None,
                          duration_ms: int | None = None, tokens: int | None = None,
                          cost_usd: float | None = None) -> RecordResult:
        """Call right after every evaluation, failures included. The server computes the outcome (§6.3)."""
        cfg = self._validate_config(config)
        metrics = dict(metrics or {})
        problems: list[str] = []
        if run_status not in RUN_STATUSES:
            problems.append(f"run_status: must be one of {', '.join(RUN_STATUSES)}")
        try:
            self.pack.validate("experiment.metrics", metrics, "metrics")
        except ValidationFailed as e:
            problems += e.problems
        for k, v in metrics.items():
            if isinstance(v, float) and not math.isfinite(v):
                problems.append(f"metrics.{k}: NaN/Infinity not allowed; use run_status='invalid' and explain in notes")
        if notes is not None and len(notes) > 2000:
            problems.append(f"notes: {len(notes)} characters, limit 2000")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            problems.append("seed: must be an integer")
        arts = list(artifacts or [])
        problems += [f"artifacts{''.join(f'[{p}]' for p in e.absolute_path)}: {e.message}"
                     for e in _ARTIFACTS.iter_errors(arts)]
        if problems:
            raise ValidationFailed(problems)
        tid = self._rid(task_id, "task")
        key = self._key(cfg, code_ref)
        pm, direction = self.project.primary_metric, self.project.metric_direction
        value = _num(metrics.get(pm)) if (pm and run_status == "ok") else None

        with self._conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.project_id,))
            if run_status == "ok":
                dup = conn.execute(
                    "SELECT id FROM assay.experiments WHERE project_id=%s AND dedup_key=%s"
                    " AND coalesce(seed,-1)=coalesce(%s::bigint,-1) AND run_status='ok'",
                    (self.project_id, key, seed)).fetchone()
                if dup:
                    return self._reject(conn, key, dup["id"])
            if run_status != "ok":
                outcome = "failed"
            elif value is None:
                outcome = "unscored"
            else:
                b = conn.execute("SELECT primary_value FROM assay.v_best WHERE project_id=%s",
                                 (self.project_id,)).fetchone()
                eps = self.project.improve_epsilon
                if b is None:
                    outcome = "improved"
                else:
                    best = b["primary_value"]
                    better = value > best + eps if direction == "max" else value < best - eps
                    worse = value < best - eps if direction == "max" else value > best + eps
                    outcome = "improved" if better else ("worse" if worse else "no_change")
            try:
                with conn.transaction():
                    r = conn.execute(
                        """INSERT INTO assay.experiments(project_id,author,task_id,config,code_ref,dedup_key,seed,
                             run_status,metrics,primary_value,outcome,notes,artifacts,trace_ref,duration_ms,tokens,
                             cost_usd) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (self.project_id, self.agent, tid, Jsonb(cfg), code_ref, key, seed, run_status,
                         Jsonb(metrics), value, outcome, notes, Jsonb(arts),
                         Jsonb(trace_ref) if trace_ref is not None else None, duration_ms, tokens, cost_usd),
                    ).fetchone()
            except psycopg.errors.UniqueViolation:
                dup = conn.execute(
                    "SELECT id FROM assay.experiments WHERE project_id=%s AND dedup_key=%s"
                    " AND coalesce(seed,-1)=coalesce(%s::bigint,-1) AND run_status='ok'",
                    (self.project_id, key, seed)).fetchone()
                return self._reject(conn, key, dup["id"] if dup else None)
        return RecordResult(id=r["id"], outcome=outcome, is_new_best=outcome == "improved",
                            duplicate_of=None, dedup_key=key, primary_value=value)

    def _reject(self, conn: psycopg.Connection[Any], key: str, existing: UUID | None) -> RecordResult:
        conn.execute("INSERT INTO assay.dedup_hits(project_id,kind,dedup_key,existing_id,agent_id)"
                     " VALUES (%s,'rejected',%s,%s,%s)", (self.project_id, key, existing, self.agent))
        return RecordResult(id=None, outcome=None, is_new_best=False, duplicate_of=existing, dedup_key=key)

    # ------------------------------------------------------------------ claims, links, sources

    def propose_claim(self, kind: str, statement: str, payload: dict[str, Any] | None = None,
                      tags: Sequence[str] | None = None, task_id: str | UUID | None = None,
                      evidence: Sequence[tuple[str | UUID, str]] | None = None) -> ClaimResult:
        """Propose a claim. An identical statement returns duplicate_of, and the evidence is attached to the
        existing claim instead (independent evidence counts as replication, §9.3)."""
        return self._propose_claim(kind, statement, payload, tags, task_id, evidence)

    def _propose_claim(self, kind: str, statement: str, payload: dict[str, Any] | None = None,
                       tags: Sequence[str] | None = None, task_id: str | UUID | None = None,
                       evidence: Sequence[tuple[str | UUID, str]] | None = None, *,
                       _dedup_hash: str | None = None) -> ClaimResult:
        problems = []
        if kind not in self.pack.claim_kinds:
            problems.append(f"kind: {kind!r} is not one of {', '.join(self.pack.claim_kinds)}")
        statement = (statement or "").strip()
        if not 1 <= len(statement) <= 500:
            problems.append(f"statement: must be 1-500 characters (got {len(statement)})")
        payload = dict(payload or {})
        if kind in self.pack.claim_kinds:
            try:
                self.pack.validate(f"claim.{kind}", payload, "payload")
            except ValidationFailed as e:
                problems += e.problems
        try:
            canonical_json(payload)
        except ValidationFailed as e:
            problems += e.problems
        ev = []
        for i, item in enumerate(evidence or []):
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                problems.append(f"evidence[{i}]: must be [id, relation]")
                continue
            if item[1] not in RELATIONS:
                problems.append(f"evidence[{i}].relation: must be one of {', '.join(RELATIONS)}")
                continue
            ev.append(item)
        if problems:
            raise ValidationFailed(problems)
        tid = self._rid(task_id, "task")
        resolved = [(self.resolve(i), rel) for i, rel in ev]
        h = _dedup_hash if _dedup_hash is not None else statement_hash(statement)
        with self._conn() as conn:
            r = conn.execute(
                "INSERT INTO assay.claims(project_id,kind,statement,statement_hash,payload,tags,author,task_id)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id, statement_hash) DO NOTHING RETURNING id",
                (self.project_id, kind, statement, h, Jsonb(payload), list(tags or []), self.agent, tid),
            ).fetchone()
            dup = None
            if r is None:
                dup = conn.execute("SELECT id FROM assay.claims WHERE project_id=%s AND statement_hash=%s",
                                   (self.project_id, h)).fetchone()["id"]
            cid: UUID | None = r["id"] if r else dup
            assert cid is not None
            links = [self._link(conn, k, i, rel, "claim", cid, None) for (k, i), rel in resolved]
        return ClaimResult(id=cid, duplicate_of=dup, link_ids=links)

    def _link(self, conn: psycopg.Connection[Any], st: str, sid: UUID, rel: str, dt: str, did: UUID,
              note: str | None) -> UUID:
        if st not in ("claim", "experiment", "source", "task") or dt not in ("claim", "experiment", "source", "task"):
            raise AssayError("links connect claims, experiments, sources and tasks")
        if sid == did:
            raise AssayError("a record cannot link to itself")
        r = conn.execute(
            "INSERT INTO assay.links(project_id,src_type,src_id,relation,dst_type,dst_id,note,author)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id, src_id, relation, dst_id) DO NOTHING"
            " RETURNING id", (self.project_id, st, sid, rel, dt, did, note, self.agent)).fetchone()
        if r is None:
            r = conn.execute("SELECT id FROM assay.links WHERE project_id=%s AND src_id=%s AND relation=%s"
                             " AND dst_id=%s", (self.project_id, sid, rel, did)).fetchone()
        assert r is not None
        return r["id"]  # type: ignore[no-any-return]

    def link(self, src: str | UUID, relation: str, dst: str | UUID, note: str | None = None) -> UUID:
        if relation not in RELATIONS:
            raise ValidationFailed([f"relation: must be one of {', '.join(RELATIONS)}"])
        if note is not None and len(note) > 500:
            raise ValidationFailed(["note: at most 500 characters"])
        kinds = ("claim", "experiment", "source", "task")
        st, sid = self.resolve(src, kinds)
        dt, did = self.resolve(dst, kinds)
        with self._conn() as conn:
            return self._link(conn, st, sid, relation, dt, did, note)

    def add_source(self, uri: str, title: str | None = None, locator: str | None = None,
                   sha256: str | None = None) -> UUID:
        if not uri:
            raise ValidationFailed(["uri: required"])
        with self._conn() as conn:
            r = conn.execute("INSERT INTO assay.sources(project_id,uri,title,locator,sha256,author)"
                             " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                             (self.project_id, uri, title, locator, sha256, self.agent)).fetchone()
        return r["id"]  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ decisions

    def claim_status(self, claim_id: str | UUID) -> str:
        cid = self._rid(claim_id, "claim")
        with self._conn() as conn:
            r = conn.execute("SELECT status FROM assay.v_claim_status WHERE id=%s", (cid,)).fetchone()
        return str(r["status"])

    def decide(self, target: str | UUID, new_status: str, oracle: str, rationale: str,
               evidence_ids: Sequence[str | UUID] = (), oracle_detail: dict[str, Any] | None = None,
               force: bool = False) -> Decision:
        """Write a status transition (orchestrator/human only, P2). Claims: §6.1 graph. Experiments:
        only 'invalidated'. 'verified' needs a non-LLM oracle and evidence (P4); oracle 'rerun' also needs
        the replication rule (§6.1). force=True permits a transition outside the claim state graph."""
        self._require_decider()
        problems = []
        if oracle not in ORACLES:
            problems.append(f"oracle: must be one of {', '.join(ORACLES)}")
        if not rationale or len(rationale) > 1000:
            problems.append("rationale: required, at most 1000 characters")
        if problems:
            raise ValidationFailed(problems)
        tkind, tid = self.resolve(target, ["claim", "experiment"])
        resolved = [self.resolve(e) for e in evidence_ids]
        ev = [eid for _, eid in resolved]
        if tkind == "experiment":
            if new_status != "invalidated":
                raise InvalidTransition("the only decision on an experiment is 'invalidated'")
        else:
            if new_status not in CLAIM_STATUSES:
                raise InvalidTransition(f"claim status must be one of {', '.join(CLAIM_STATUSES)}")
            if new_status == "verified":
                if oracle not in NON_LLM_ORACLES:
                    raise InvalidTransition(f"verified needs a non-LLM oracle ({', '.join(NON_LLM_ORACLES)}); "
                                            "an LLM judgement can reach 'supported' at most (P4)")
                if not ev:
                    raise InvalidTransition("verified needs at least one evidence id")
        with self._conn() as conn:
            # Share the experiment writer's lock so invalidation, outcome calculation, and
            # decision validation each observe the results of the preceding writer.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.project_id,))
            last = conn.execute(
                "SELECT new_status, created_at FROM assay.decisions"
                " WHERE project_id=%s AND target_id=%s AND target_type=%s"
                " ORDER BY created_at DESC, id DESC LIMIT 1", (self.project_id, tid, tkind)).fetchone()
            if tkind == "claim":
                cur = last["new_status"] if last else "proposed"
                if new_status not in TRANSITIONS.get(cur, set()) and not force:
                    raise InvalidTransition(f"{cur} -> {new_status} is outside the claim state graph; pass force=True "
                                            "if you mean it")
                if new_status == "verified" and oracle == "rerun":
                    self._check_rerun(conn, tid, {eid for kind, eid in resolved if kind == "experiment"})
            r = conn.execute(
                "INSERT INTO assay.decisions(project_id,target_type,target_id,new_status,oracle,oracle_detail,"
                "rationale,evidence_ids,decided_by,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "greatest(clock_timestamp(), %s::timestamptz + interval '1 microsecond')) RETURNING *",
                (self.project_id, tkind, tid, new_status, oracle, Jsonb(oracle_detail or {}), rationale, ev,
                 self.agent, last["created_at"] if last else None)).fetchone()
        return Decision(**{k: r[k] for k in Decision.model_fields})

    def _check_rerun(self, conn: psycopg.Connection[Any], claim_id: UUID, experiment_ids: set[UUID]) -> None:
        runs = conn.execute(
            "SELECT dedup_key, author, seed FROM assay.experiments_valid"
            " WHERE project_id=%s AND id=ANY(%s) AND run_status='ok'",
            (self.project_id, list(experiment_ids))).fetchall()
        if not runs or len(runs) != len(experiment_ids) or len({run["dedup_key"] for run in runs}) != 1:
            raise InvalidTransition("rerun oracle needs valid successful experiment evidence with the same config "
                                    "and code reference.")
        original = conn.execute(
            """WITH prior AS (
                  SELECT evidence_ids FROM assay.decisions
                  WHERE project_id=%(project)s AND target_id=%(claim)s
                    AND target_type='claim' AND new_status='supported'
                  ORDER BY created_at DESC, id DESC LIMIT 1
                ), cited AS (
                  SELECT e.id, e.dedup_key, e.author, e.created_at FROM assay.experiments e
                  JOIN prior p ON e.id=ANY(p.evidence_ids)
                  WHERE e.project_id=%(project)s AND e.run_status='ok'
                ), original_candidates AS (
                  SELECT * FROM cited
                  UNION ALL
                  SELECT e.id, e.dedup_key, e.author, e.created_at FROM assay.experiments e
                  JOIN assay.links l ON l.project_id=e.project_id AND l.src_id=e.id
                  WHERE e.project_id=%(project)s AND e.run_status='ok' AND l.src_type='experiment'
                    AND l.dst_type='claim' AND l.dst_id=%(claim)s AND l.relation='supports'
                    AND NOT EXISTS (SELECT 1 FROM cited)
                )
                SELECT author FROM original_candidates WHERE dedup_key=%(key)s
                ORDER BY created_at, id LIMIT 1""",
            {"project": self.project_id, "claim": claim_id, "key": runs[0]["dedup_key"]}).fetchone()
        if original is None:
            raise InvalidTransition("rerun evidence must match the config and code reference of an original "
                                    "supporting experiment for the claim.")
        st = self.project.settings
        min_seeds, min_authors = int(st.get("rerun_min_seeds", 3)), int(st.get("rerun_min_authors", 2))
        seeds = {run["seed"] for run in runs if run["seed"] is not None}
        authors = {run["author"] for run in runs}
        if len(seeds) < min_seeds or len(authors) < min_authors or not (authors - {original["author"]}):
            raise InvalidTransition(
                f"rerun oracle needs >= {min_seeds} distinct seeds and >= {min_authors} distinct authors "
                f"including a rerun by someone other than {original['author']}; "
                f"got {len(seeds)} seeds, {len(authors)} authors. Create a replication task.")

    # ------------------------------------------------------------------ reads

    def get(self, ref: str | UUID) -> dict[str, Any]:
        kind, rid = self.resolve(ref)
        with self._conn() as conn:
            rec = conn.execute(psql.SQL("SELECT * FROM assay.{} WHERE id=%s").format(psql.Identifier(TABLE[kind])),
                               (rid,)).fetchone()
            out: dict[str, Any] = {"kind": kind, "id": short(kind, rid), "record": rec}
            if kind in ("claim", "experiment", "source", "task"):
                out["links_out"] = [
                    {"id": short("link", lk["id"]), "relation": lk["relation"],
                     "dst": short(lk["dst_type"], lk["dst_id"]), "note": lk["note"], "author": lk["author"]}
                    for lk in conn.execute("SELECT * FROM assay.links WHERE src_id=%s ORDER BY created_at", (rid,))]
                out["links_in"] = [
                    {"id": short("link", lk["id"]), "relation": lk["relation"],
                     "src": short(lk["src_type"], lk["src_id"]), "note": lk["note"], "author": lk["author"]}
                    for lk in conn.execute("SELECT * FROM assay.links WHERE dst_id=%s ORDER BY created_at", (rid,))]
            if kind in ("claim", "experiment"):
                out["decisions"] = [
                    {"id": short("decision", d["id"]), "status": d["new_status"], "oracle": d["oracle"],
                     "by": d["decided_by"], "rationale": d["rationale"], "at": d["created_at"]}
                    for d in conn.execute("SELECT * FROM assay.decisions WHERE target_id=%s ORDER BY created_at",
                                          (rid,))]
            if kind == "claim":
                out["status"] = conn.execute("SELECT * FROM assay.v_claim_status WHERE id=%s", (rid,)).fetchone()
            if kind == "experiment":
                out["invalidated"] = any(d["status"] == "invalidated" for d in out["decisions"])
            if kind == "task":
                out["events"] = conn.execute(
                    "SELECT event, agent_id, detail, created_at FROM assay.task_events WHERE task_id=%s ORDER BY id",
                    (rid,)).fetchall()
        return jsonable(out)  # type: ignore[no-any-return]

    def status(self) -> dict[str, Any]:
        p = self.project_id
        with self._conn() as conn:
            best = conn.execute("SELECT id, author, primary_value, created_at FROM assay.v_best WHERE project_id=%s",
                                (p,)).fetchone()
            exp = conn.execute(
                "SELECT count(*) AS total, count(*) FILTER (WHERE run_status='ok') AS ok,"
                " count(*) FILTER (WHERE outcome='improved') AS improved FROM assay.experiments WHERE project_id=%s",
                (p,)).fetchone()
            inval = conn.execute("SELECT count(DISTINCT target_id) AS n FROM assay.decisions WHERE project_id=%s"
                                 " AND new_status='invalidated'", (p,)).fetchone()["n"]
            dd = conn.execute("SELECT rejected, avoided FROM assay.v_dedup_savings WHERE project_id=%s",
                              (p,)).fetchone()
            claims = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM assay.v_claim_status WHERE project_id=%s GROUP BY status", (p,))}
            tasks = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM assay.tasks WHERE project_id=%s GROUP BY status", (p,))}
            claimable = conn.execute("SELECT count(*) AS n FROM assay.v_open_tasks WHERE project_id=%s",
                                     (p,)).fetchone()["n"]
            active = [r["agent"] for r in conn.execute(
                "SELECT agent FROM assay.v_agent_stats WHERE project_id=%s"
                " AND last_active_at > now() - interval '15 minutes' AND role <> 'harness' ORDER BY agent", (p,))]
        pr = self.project
        out: dict[str, Any] = jsonable({
            "project": p, "goal": pr.goal, "metric": pr.primary_metric, "direction": pr.metric_direction,
            "best": None if best is None else {"id": short("experiment", best["id"]), "value": best["primary_value"],
                                               "author": best["author"], "at": best["created_at"]},
            "experiments": {**exp, "invalidated": inval},
            "dedup": dd, "claims": claims, "tasks": {**tasks, "claimable": claimable}, "active_agents": active,
        })
        return out

    def search(self, text: str | None = None, kind: str | None = None, status: str | None = None,
               tags: Sequence[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """One-line summaries. kind: claim|experiment|task or a claim kind (hypothesis, ...).
        status: claim status, task status or experiment outcome."""
        limit = max(1, min(int(limit), 200))
        like = f"%{text}%" if text else None
        tg = list(tags or [])
        claim_kind = kind if kind in self.pack.claim_kinds else None
        want = {"claim"} if claim_kind else ({kind} if kind else {"claim", "experiment", "task"})
        out: list[dict[str, Any]] = []
        with self._conn() as conn:
            if "claim" in want:
                for r in conn.execute(
                    "SELECT id, kind, status, statement, n_support, n_refute, created_at FROM assay.v_claim_status"
                    " WHERE project_id=%(p)s AND (%(l)s::text IS NULL OR statement ILIKE %(l)s)"
                    " AND (%(k)s::text IS NULL OR kind=%(k)s) AND (%(s)s::text IS NULL OR status=%(s)s)"
                    " AND (cardinality(%(t)s::text[])=0 OR tags && %(t)s::text[])"
                    " ORDER BY created_at DESC LIMIT %(n)s",
                    {"p": self.project_id, "l": like, "k": claim_kind, "s": status, "t": tg, "n": limit}):
                    out.append({"id": short("claim", r["id"]), "kind": "claim", "at": r["created_at"],
                                "line": f"[{r['status']}] {r['kind']}: {r['statement']} "
                                        f"(+{r['n_support']}/-{r['n_refute']})"})
            if "experiment" in want:
                for r in conn.execute(
                    "SELECT e.id, e.author, e.outcome, e.primary_value, e.config, e.notes, e.created_at"
                    " FROM assay.experiments_valid e LEFT JOIN assay.tasks t ON t.id = e.task_id"
                    " WHERE e.project_id=%(p)s AND (%(l)s::text IS NULL OR e.notes ILIKE %(l)s"
                    "   OR e.config::text ILIKE %(l)s) AND (%(s)s::text IS NULL OR e.outcome=%(s)s)"
                    " AND (cardinality(%(t)s::text[])=0 OR t.tags && %(t)s::text[])"
                    " ORDER BY e.created_at DESC LIMIT %(n)s",
                    {"p": self.project_id, "l": like, "s": status, "t": tg, "n": limit}):
                    val = "" if r["primary_value"] is None else f" {r['primary_value']:.6g}"
                    out.append({"id": short("experiment", r["id"]), "kind": "experiment", "at": r["created_at"],
                                "line": f"{r['outcome']}{val} by {r['author']}: {config_line(r['config'])}"
                                        + (f" | {r['notes'][:80]}" if r["notes"] else "")})
            if "task" in want:
                for r in conn.execute(
                    "SELECT id, title, status, lease_owner, tags, created_at FROM assay.tasks WHERE project_id=%(p)s"
                    " AND (%(l)s::text IS NULL OR title ILIKE %(l)s) AND (%(s)s::text IS NULL OR status=%(s)s)"
                    " AND (cardinality(%(t)s::text[])=0 OR tags && %(t)s::text[])"
                    " ORDER BY created_at DESC LIMIT %(n)s",
                    {"p": self.project_id, "l": like, "s": status, "t": tg, "n": limit}):
                    out.append({"id": short("task", r["id"]), "kind": "task", "at": r["created_at"],
                                "line": f"[{r['status']}] {r['title']}"
                                        + (f" ({r['lease_owner']})" if r["status"] == "claimed" else "")})
        out.sort(key=lambda x: x["at"], reverse=True)
        return [{k: v for k, v in jsonable(o).items() if k != "at"} for o in out[:limit]]

    def sql(self, query: str) -> dict[str, Any]:
        """Read-only SQL over the per-project v_* views (orchestrator tool, §7.3)."""
        q = query.strip().rstrip(";").strip()
        if not q:
            raise AssayError("empty query")
        if ";" in q:
            raise AssayError("one statement only (no ';')")
        if not re.match(r"^(select|with|table|values)\b", q, re.IGNORECASE):
            raise AssayError("only SELECT queries over v_* views are allowed")
        if _FORBIDDEN_SQL.search(q):
            raise AssayError("query uses a forbidden function or command")
        conn, via_set_role = self._reader_conn()
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute("SET LOCAL statement_timeout = '2s'")
                conn.execute("SET LOCAL search_path = assay_q")
                conn.execute("SELECT set_config('assay.project', %s, true)", (self.project_id,))
                if via_set_role:
                    conn.execute("SET LOCAL ROLE assay_reader")
                try:
                    # Describe the original query without fetching any rows. Experiments can contain
                    # configurations larger than the entire SQL budget.
                    with conn.cursor(name="assay_columns") as columns:
                        columns.execute(q.encode("utf-8"))
                        cols = [c.name for c in (columns.description or [])]
                    bounded_query = psql.SQL(
                        "SELECT CASE WHEN octet_length(convert_to(row_data, 'UTF8')) <= {}"
                        " THEN row_data END AS row_data"
                        " FROM (SELECT row_to_json(assay_result.*)::text AS row_data"
                        " FROM (\n{}\n) AS assay_result) AS assay_serialized"
                    ).format(psql.Literal(SQL_TOKEN_LIMIT * 4), psql.SQL(q))
                    with conn.cursor(name="assay_rows", scrollable=False) as cursor:
                        cursor.execute(bounded_query)
                        fetched = cursor.fetchmany(SQL_ROW_LIMIT)
                        remaining = conn.execute(
                            psql.SQL("MOVE FORWARD ALL FROM {}").format(psql.Identifier(cursor.name))
                        ).rowcount
                except psycopg.errors.QueryCanceled as e:
                    raise AssayError("query exceeded the 2s statement timeout") from e
                except psycopg.errors.InsufficientPrivilege as e:
                    raise AssayError(f"permission denied: only the v_* views are readable ({e.diag.message_primary})"
                                     ) from e
                except psycopg.Error as e:
                    raise AssayError(f"SQL error: {e.diag.message_primary or e}") from e
        finally:
            conn.close()
        total = len(fetched) + remaining
        rows: list[dict[str, Any]] = []
        response: dict[str, Any] = {"columns": cols, "rows": rows, "row_count": total, "returned": 0,
                                    "truncated": total > 0}
        if estimate_tokens(json.dumps(response, ensure_ascii=False)) > SQL_TOKEN_LIMIT:
            raise AssayError("SQL column metadata exceeds the 4,000-token limit; select fewer columns")
        for row in fetched:
            if row["row_data"] is None:
                break
            rows.append(json.loads(row["row_data"]))
            response.update(returned=len(rows), truncated=len(rows) < total)
            if estimate_tokens(json.dumps(response, ensure_ascii=False)) > SQL_TOKEN_LIMIT:
                rows.pop()
                response.update(returned=len(rows), truncated=True)
                break
        return response

    def _reader_conn(self) -> tuple[psycopg.Connection[Any], bool]:
        """Connect as assay_reader so the session user itself is the reader. If that is impossible
        (a managed Postgres with password auth and no reader password), fall back to SET ROLE."""
        import os

        kw: dict[str, Any] = {"row_factory": dict_row, "autocommit": False, "user": "assay_reader"}
        if pw := os.environ.get("ASSAY_READER_PASSWORD"):
            kw["password"] = pw
        try:
            return psycopg.connect(self.url, **kw), False
        except psycopg.OperationalError:
            return psycopg.connect(self.url, row_factory=dict_row, autocommit=False), True

    def brief(self, task_id: str | UUID | None = None, budget_tokens: int = 2000) -> str:
        from .brief import compile_brief

        tid = self._rid(task_id, "task")
        return compile_brief(self, tid, budget_tokens)

    def export(self) -> Iterator[dict[str, Any]]:
        """The whole project as an append-only event stream, oldest first."""
        tables = ["agents", "tasks", "task_events", "sources", "experiments", "claims", "links", "decisions",
                  "dedup_hits", "brief_log"]
        with self._conn() as conn:
            proj = conn.execute("SELECT * FROM assay.projects WHERE id=%s", (self.project_id,)).fetchone()
            yield {"type": "project", "record": jsonable(proj)}
            events: list[tuple[Any, str, dict[str, Any]]] = []
            for t in tables:
                for r in conn.execute(psql.SQL("SELECT * FROM assay.{} WHERE project_id=%s").format(
                        psql.Identifier(t)), (self.project_id,)):
                    events.append((r["created_at"], t, r))
        events.sort(key=lambda e: (e[0], tables.index(e[1])))
        for _, t, r in events:
            yield {"type": t.rstrip("s") if t not in ("dedup_hits", "brief_log") else t, "record": jsonable(r)}

    def sql_internal(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        """Full-privilege read for adapters (viewer). Not exposed to agents."""
        with self._conn() as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[no-any-return]


def config_line(cfg: Any, max_len: int = 90) -> str:
    if not isinstance(cfg, dict):
        return str(cfg)[:max_len]
    parts = []
    for k in sorted(cfg):
        v = cfg[k]
        sv = json.dumps(v, ensure_ascii=False, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v)
        parts.append(f"{k}={sv}")
    s = ", ".join(parts)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def schema_version_ok(url: str) -> bool:
    from .db.migrate import current

    return current(url) == migrate_head()
