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
from statistics import fmean
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
    BenchbookError,
    Conflict,
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
    r"pg_terminate_backend|pg_cancel_backend|pg_settings|query_to_xml\w*|cursor_to_xml\w*|table_to_xml\w*|"
    r"schema_to_xml\w*|database_to_xml\w*|pg_file\w*|pg_stat_file)\b"
    # U&"..." / U&'...' spell any identifier or string with escapes (U&"set\005fconfig" is set_config)
    r"|\bu&\s*[\"']",
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
    if isinstance(v, (bytes, memoryview)):
        import base64

        return base64.b64encode(bytes(v)).decode("ascii")
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
    try:
        f = float(v)
    except OverflowError:
        return None
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
        """Open a project's ledger. Project comes from the argument, BENCHBOOK_PROJECT or .benchbook/config.toml.
        Agent comes from the argument or BENCHBOOK_AGENT; there is no default, so a process that forgot to name
        itself cannot act as 'human' by accident. An unknown agent is registered with `role`
        (default worker; 'human' defaults to the human role)."""
        import os

        project = project or localcfg.load().project
        if not project:
            raise BenchbookError("no project: pass project=, set BENCHBOOK_PROJECT, or run `benchbook init`")
        agent = agent or os.environ.get("BENCHBOOK_AGENT")
        if not agent:
            raise BenchbookError("no agent name: set BENCHBOOK_AGENT=<name> or pass --agent <name> "
                             "(a person deciding by hand uses `benchbook decide ... --as human`)")
        url = cls.ensure_schema(database_url)
        with cls._pool(url).connection() as conn:
            row = conn.execute("SELECT * FROM benchbook.projects WHERE id = %s", (project,)).fetchone()
            if row is None:
                raise NotFound(f"project {project!r} does not exist; run `benchbook init --project {project}`")
            p = Project(**row)
            actual = cls._ensure_agent(conn, p.id, agent, role)
        return cls(url, p, agent, actual)

    @staticmethod
    def _ensure_agent(conn: psycopg.Connection[Any], project_id: str, agent: str, role: str | None,
                      model: str | None = None) -> str:
        if not agent or len(agent) > 100:
            raise BenchbookError("agent name must be 1-100 characters")
        want = role or ("human" if agent == "human" else "worker")
        if want not in ROLES:
            raise BenchbookError(f"role must be one of {ROLES}")
        conn.execute(
            "INSERT INTO benchbook.agents(project_id, id, role, model) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (project_id, agent, want, model),
        )
        r = conn.execute("SELECT role FROM benchbook.agents WHERE project_id=%s AND id=%s",
                         (project_id, agent)).fetchone()
        assert r is not None
        return str(r["role"])

    @classmethod
    def init_project(cls, id: str, title: str | None = None, goal: str = "", metric: str | None = None,
                     direction: str | None = None, epsilon: float = 0.0,
                     config_schema: dict[str, Any] | None = None, settings: dict[str, Any] | None = None,
                     database_url: str | None = None, pack: str = "core") -> tuple[Project, bool]:
        """Create a project (idempotent). Registers lead (orchestrator), human (human), hook (harness).
        Returns (project, created)."""
        if (metric is None) != (direction is None):
            raise BenchbookError("--metric and --direction go together")
        if direction not in (None, "max", "min"):
            raise BenchbookError("direction must be max or min")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", id):
            raise BenchbookError("project id must be a lowercase slug: [a-z0-9][a-z0-9_-]*")
        load_pack(pack)  # unknown packs fail before anything is written
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
                "INSERT INTO benchbook.projects(id,title,goal,primary_metric,metric_direction,improve_epsilon,pack,"
                "settings) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING RETURNING *",
                (id, title or id, goal or "(no goal set)", metric, direction, epsilon, pack, Jsonb(st)),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute("SELECT * FROM benchbook.projects WHERE id=%s", (id,)).fetchone()
            for a, r in (("lead", "orchestrator"), ("human", "human"), ("hook", "harness")):
                cls._ensure_agent(conn, id, a, r)
        return Project(**row), created

    def refresh(self) -> Project:
        """Re-read the project row (settings change while agents run)."""
        with self._conn() as conn:
            self.project = Project(**conn.execute("SELECT * FROM benchbook.projects WHERE id=%s",
                                                  (self.project_id,)).fetchone())
        return self.project

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a JSON merge patch (RFC 7386) to the project settings (orchestrator/human only).
        Every change is kept as an append-only settings_events row."""
        self._require_decider()
        if not isinstance(patch, dict) or not patch:
            raise ValidationFailed(["patch: must be a non-empty JSON object"])
        canonical_json(patch)
        with self._conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.project_id,))
            row = conn.execute("SELECT settings FROM benchbook.projects WHERE id=%s FOR UPDATE",
                               (self.project_id,)).fetchone()
            new: dict[str, Any] = merge_patch(row["settings"], patch)
            if "config_schema" in new:
                try:
                    Draft202012Validator.check_schema(new["config_schema"])
                except Exception as e:  # jsonschema.SchemaError
                    raise ValidationFailed([f"config_schema: {e}"]) from e
            conn.execute("UPDATE benchbook.projects SET settings=%s WHERE id=%s", (Jsonb(new), self.project_id))
            conn.execute("INSERT INTO benchbook.settings_events(project_id, agent_id, patch, settings)"
                         " VALUES (%s,%s,%s,%s)", (self.project_id, self.agent, Jsonb(patch), Jsonb(new)))
        self.refresh()
        return new

    # ------------------------------------------------------------------ blobs (C2)

    def put_blob(self, data: bytes, media_type: str = "application/octet-stream") -> str:
        """Store bytes content-addressed; returns benchbook://blob/<sha256>. Re-putting returns the same URI."""
        import hashlib

        if not isinstance(data, (bytes, bytearray)):
            raise ValidationFailed(["data: must be bytes"])
        limit = int(self.project.settings.get("blob_max_bytes", 1_000_000))
        if len(data) > limit:
            raise ValidationFailed([f"blob: {len(data)} bytes, limit {limit} (settings.blob_max_bytes)"])
        digest = hashlib.sha256(data).hexdigest()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO benchbook.blobs(project_id, sha256, bytes, size, media_type, author)"
                " VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id, sha256) DO NOTHING",
                (self.project_id, digest, bytes(data), len(data), media_type, self.agent))
        return BLOB_PREFIX + digest

    def get_blob(self, uri: str) -> bytes:
        digest = uri[len(BLOB_PREFIX):] if uri.startswith(BLOB_PREFIX) else uri
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise NotFound(f"not a blob uri: {uri!r} (expected {BLOB_PREFIX}<sha256>)")
        with self._conn() as conn:
            r = conn.execute("SELECT bytes FROM benchbook.blobs WHERE project_id=%s AND sha256=%s",
                             (self.project_id, digest)).fetchone()
        if r is None:
            raise NotFound(f"no blob {digest[:12]}… in project {self.project_id}")
        return bytes(r["bytes"])

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
            raise NotFound(f"not an benchbook id: {ref!r} (expected e:/c:/t:/s:/d:/l: + hex, or a uuid)")
        pfx, body = m.groups()
        search = [KIND_BY_PREFIX[pfx]] if pfx else list(kinds or TABLE.keys())
        if kinds is not None:
            search = [k for k in search if k in kinds]
            if not search:
                raise NotFound(f"{ref!r} is not one of: {', '.join(kinds)}")
        hits: list[tuple[str, UUID]] = []
        with self._conn() as conn:
            for kind in search:
                q = psql.SQL("SELECT id FROM benchbook.{} WHERE project_id = %s AND ").format(
                    psql.Identifier(TABLE[kind]))
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
                    "INSERT INTO benchbook.tasks(project_id,title,spec,tags,priority,depends_on,created_by,"
                    "max_attempts)"
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
            "INSERT INTO benchbook.task_events(project_id,task_id,event,agent_id,detail) VALUES (%s,%s,%s,%s,%s)",
            (self.project_id, task_id, event, agent if agent is not None else self.agent, Jsonb(detail)),
        )

    def lease_default(self) -> int:
        """settings.lease_seconds (default 900): the lease a claim gets when the caller names none."""
        with self._conn() as conn:
            return self._read_lease_default(conn)

    def _read_lease_default(self, conn: psycopg.Connection[Any]) -> int:
        row = conn.execute("SELECT settings->'lease_seconds' AS seconds FROM benchbook.projects WHERE id=%s",
                           (self.project_id,)).fetchone()
        assert row is not None
        seconds = _num(row["seconds"])
        return int(seconds) if seconds is not None and seconds >= 1 else 900

    def next_task(self, tags: Sequence[str] | None = None, lease_seconds: int | None = None,
                  spec_match: dict[str, Any] | None = None, task_id: str | UUID | None = None) -> Task | None:
        """Atomically claim the highest-priority claimable task (spec §10). None if nothing is claimable.
        Tasks whose spec.exclude_agents lists this agent are skipped (C4). A task with
        spec.distinct_agents_for = "<key>" is skipped when this agent already claimed another task with the
        same spec[<key>] (one agent cannot replicate the same origin twice). spec_match keeps only tasks
        whose spec contains the given JSON (jsonb @>). task_id claims that one task if it is claimable."""
        tid = self._rid(task_id, "task")
        if lease_seconds is not None and lease_seconds <= 0:
            raise BenchbookError("lease_seconds must be positive")
        if spec_match is not None and not isinstance(spec_match, dict):
            raise ValidationFailed(["spec_match: must be a JSON object"])
        with self._conn() as conn:
            # Lock the agent before selecting a task, so a parallel claim sees its
            # committed event even if it would lock a different task row.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                         ("benchbook:task-claim:" + self.project_id, self.agent))
            if lease_seconds is None:
                lease_seconds = self._read_lease_default(conn)
            # Exhausted tasks whose lease ran out become failed here; there is no scheduler (P8).
            conn.execute(
                """WITH f AS (
                     UPDATE benchbook.tasks SET status='failed', updated_at=statement_timestamp()
                     WHERE project_id=%(p)s AND status='claimed' AND lease_expires_at < statement_timestamp()
                       AND attempts >= max_attempts
                     RETURNING id, lease_owner)
                   INSERT INTO benchbook.task_events(project_id, task_id, event, agent_id, detail)
                   SELECT %(p)s, id, e.event, lease_owner, '{"reason":"lease expired, attempts exhausted"}'
                   FROM f CROSS JOIN (VALUES ('expired'), ('failed')) e(event)""",
                {"p": self.project_id},
            )
            row = conn.execute(
                """WITH c AS (
                     SELECT t.id, t.status AS prev_status, t.lease_owner AS prev_owner FROM benchbook.tasks t
                     WHERE t.project_id = %(p)s
                       AND (t.status = 'open' OR (t.status = 'claimed' AND t.lease_expires_at < statement_timestamp()))
                       AND t.attempts < t.max_attempts
                       AND (cardinality(%(tags)s::text[]) = 0 OR t.tags && %(tags)s::text[])
                       AND NOT coalesce(t.spec -> 'exclude_agents' ? %(me)s, false)
                       AND t.spec @> %(match)s::jsonb
                       AND (%(tid)s::uuid IS NULL OR t.id = %(tid)s::uuid)
                       AND NOT (t.spec ? 'distinct_agents_for' AND EXISTS (
                             SELECT 1 FROM benchbook.tasks o JOIN benchbook.task_events ev ON ev.task_id = o.id
                             WHERE o.project_id = t.project_id AND o.id <> t.id AND ev.event = 'claimed'
                               AND ev.agent_id = %(me)s
                               AND o.spec ->> 'distinct_agents_for' = t.spec ->> 'distinct_agents_for'
                               AND o.spec ->> (t.spec ->> 'distinct_agents_for')
                                   = t.spec ->> (t.spec ->> 'distinct_agents_for')))
                       AND NOT EXISTS (SELECT 1 FROM benchbook.tasks d
                                       WHERE d.id = ANY (t.depends_on) AND d.status <> 'done')
                     ORDER BY t.priority DESC, t.created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1)
                   UPDATE benchbook.tasks t
                   SET status = 'claimed', lease_owner = %(me)s,
                       lease_expires_at = statement_timestamp() + make_interval(secs => %(secs)s),
                       attempts = t.attempts + 1, updated_at = statement_timestamp()
                   FROM c WHERE t.id = c.id
                   RETURNING t.*, c.prev_status, c.prev_owner""",
                {"p": self.project_id, "tags": list(tags or []), "me": self.agent, "secs": lease_seconds,
                 "match": Jsonb(spec_match or {}), "tid": tid},
            ).fetchone()
            if row is None:
                return None
            prev_status, prev_owner = row.pop("prev_status"), row.pop("prev_owner")
            if prev_status == "claimed":
                self._event(conn, row["id"], "expired", {"reason": "lease expired"}, agent=prev_owner)
            self._event(conn, row["id"], "claimed", {"lease_seconds": lease_seconds, "attempt": row["attempts"]})
            return Task(**row)

    def claim_task(self, task_id: str | UUID, lease_seconds: int | None = None) -> Task:
        """Claim one task by id (the lead hands a specific task to a specific worker). Raises when it is
        not claimable by this agent (done, leased by someone else, excluded, or waiting on dependencies)."""
        t = self.next_task(lease_seconds=lease_seconds, task_id=task_id)
        if t is None:
            cur = self.get_task(task_id)
            who = f" by {cur.lease_owner}" if cur.status == "claimed" else ""
            raise Conflict(f"{cur.short_id} is not claimable by {self.agent}: status {cur.status}{who} "
                           f"(attempts {cur.attempts}/{cur.max_attempts}; exclusions and dependencies also apply)",
                           existing=cur.id, agent=cur.lease_owner)
        return t

    def create_intent(self, title: str, spec: dict[str, Any] | None = None, tags: Sequence[str] | None = None,
                      lease_seconds: int = 900, exclusive: Sequence[str] = ()) -> Task:
        """Announce work you are about to do (C3): a task created already claimed by you, tagged 'intent'.
        Needs settings.worker_intents for workers. For each spec field named in `exclusive`, another live
        intent with the same value is a conflict (raises Conflict, CLI exit 2)."""
        if self.role not in DECIDERS and not self.project.settings.get("worker_intents"):
            self.refresh()
            if not self.project.settings.get("worker_intents"):
                raise PermissionDenied("intents are off for this project (settings.worker_intents=false)")
        if not isinstance(title, str) or not title.strip():
            raise ValidationFailed(["title: required non-empty string"])
        spec = dict(spec or {})
        if lease_seconds <= 0:
            raise BenchbookError("lease_seconds must be positive")
        tg = list(dict.fromkeys(["intent", *(tags or [])]))
        with self._conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("intent:" + self.project_id,))
            self._check_exclusive(conn, spec, exclusive, None)
            r = conn.execute(
                "INSERT INTO benchbook.tasks(project_id,title,spec,tags,created_by,status,lease_owner,lease_expires_at,"
                "attempts,max_attempts) VALUES (%s,%s,%s,%s,%s,'claimed',%s,now() + make_interval(secs => %s),1,1)"
                " RETURNING *",
                (self.project_id, title.strip()[:500], Jsonb(spec), tg, self.agent, self.agent, lease_seconds),
            ).fetchone()
            self._event(conn, r["id"], "created", {"intent": True})
            self._event(conn, r["id"], "claimed", {"lease_seconds": lease_seconds, "attempt": 1})
            return Task(**r)

    def update_intent(self, task_id: str | UUID, spec_patch: dict[str, Any], lease_seconds: int | None = None,
                      exclusive: Sequence[str] = ()) -> Task:
        """Merge fields into the spec of your own live intent and optionally extend its lease."""
        tid = self._rid(task_id, "task")
        with self._conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("intent:" + self.project_id,))
            cur = conn.execute("SELECT * FROM benchbook.tasks WHERE id=%s AND status='claimed' AND lease_owner=%s"
                               " AND 'intent' = ANY(tags) FOR UPDATE", (tid, self.agent)).fetchone()
            if cur is None:
                raise LeaseLost(short("task", tid))  # type: ignore[arg-type]
            spec = {**cur["spec"], **spec_patch}
            self._check_exclusive(conn, spec, exclusive, tid)
            r = conn.execute(
                "UPDATE benchbook.tasks SET spec=%s, updated_at=now(), lease_expires_at = CASE WHEN %s::int IS NULL"
                " THEN lease_expires_at ELSE now() + make_interval(secs => %s::int) END WHERE id=%s RETURNING *",
                (Jsonb(spec), lease_seconds, lease_seconds, tid)).fetchone()
            self._event(conn, tid, "updated", {"spec": spec_patch})  # type: ignore[arg-type]
            return Task(**r)

    def _check_exclusive(self, conn: psycopg.Connection[Any], spec: dict[str, Any], exclusive: Sequence[str],
                         own: UUID | None) -> None:
        for key in exclusive:
            if spec.get(key) is None:
                continue
            other = conn.execute(
                "SELECT id, lease_owner, title FROM benchbook.tasks WHERE project_id=%s AND status='claimed'"
                " AND lease_expires_at > now() AND 'intent' = ANY(tags) AND spec -> %s = %s::jsonb"
                " AND id IS DISTINCT FROM %s ORDER BY created_at LIMIT 1",
                (self.project_id, key, Jsonb(spec[key]), own)).fetchone()
            if other is not None:
                raise Conflict(f"{short('task', other['id'])} by {other['lease_owner']} is already working on this "
                               f"({key}): {other['title']}", existing=other["id"], agent=other["lease_owner"])

    def my_intents(self) -> list[Task]:
        """Your live intents, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM benchbook.tasks WHERE project_id=%s AND status='claimed' AND lease_owner=%s"
                " AND lease_expires_at > now() AND 'intent' = ANY(tags) ORDER BY created_at DESC",
                (self.project_id, self.agent)).fetchall()
        return [Task(**r) for r in rows]

    def heartbeat(self, task_id: str | UUID, lease_seconds: int | None = None) -> datetime:
        tid = self._rid(task_id, "task")
        if lease_seconds is not None and lease_seconds <= 0:
            raise BenchbookError("lease_seconds must be positive")
        with self._conn() as conn:
            if lease_seconds is None:
                lease_seconds = self._read_lease_default(conn)
            r = conn.execute(
                "UPDATE benchbook.tasks SET lease_expires_at = now() + make_interval(secs => %s), updated_at=now()"
                " WHERE id=%s AND status='claimed' AND lease_owner=%s RETURNING lease_expires_at",
                (lease_seconds, tid, self.agent),
            ).fetchone()
            if r is None:
                raise LeaseLost(short("task", tid))  # type: ignore[arg-type]
            self._event(conn, tid, "heartbeat", {})  # type: ignore[arg-type]
            return r["lease_expires_at"]  # type: ignore[no-any-return]

    def complete_task(self, task_id: str | UUID, status: str = "done", summary: str = "") -> Task:
        if status not in ("done", "failed"):
            raise BenchbookError("status must be done or failed")
        if len(summary) > 1000:
            raise ValidationFailed(["summary: at most 1000 characters (2-3 sentences); details belong in records"])
        tid = self._rid(task_id, "task")
        with self._conn() as conn:
            r = conn.execute(
                "UPDATE benchbook.tasks SET status=%s, summary=%s, lease_expires_at=NULL, updated_at=now()"
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
                "UPDATE benchbook.tasks SET status='cancelled', lease_expires_at=NULL, updated_at=now()"
                " WHERE id=%s AND status IN ('open','claimed') RETURNING *", (tid,),
            ).fetchone()
            if r is None:
                raise BenchbookError(f"{short('task', tid)} is not open or claimed")  # type: ignore[arg-type]
            self._event(conn, tid, "cancelled", {"reason": reason})  # type: ignore[arg-type]
            return Task(**r)

    def get_task(self, task_id: str | UUID) -> Task:
        tid = self._rid(task_id, "task")
        with self._conn() as conn:
            return Task(**conn.execute("SELECT * FROM benchbook.tasks WHERE id=%s", (tid,)).fetchone())

    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM benchbook.tasks WHERE project_id=%s AND (%s::text IS NULL OR status=%s)"
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
                " FROM benchbook.experiments_valid WHERE project_id=%s AND dedup_key=%s"
                " ORDER BY created_at DESC LIMIT 20", (self.project_id, key)).fetchall()
            agg = conn.execute(
                "SELECT count(DISTINCT seed) FILTER (WHERE run_status='ok') AS reps, best_value"
                " FROM benchbook.experiments_valid e LEFT JOIN benchbook.v_tried t USING (project_id, dedup_key)"
                " WHERE e.project_id=%s AND e.dedup_key=%s GROUP BY best_value",
                (self.project_id, key)).fetchone()
            if rows:
                conn.execute(
                    "INSERT INTO benchbook.dedup_hits(project_id,kind,dedup_key,existing_id,agent_id)"
                    " VALUES (%s,'avoided',%s,%s,%s)", (self.project_id, key, rows[0]["id"], self.agent))
            # The unique index also counts invalidated runs (DECISIONS.md): say so before the GPU time is spent.
            inval = conn.execute(
                "SELECT e.id, e.author, e.seed, e.run_status, e.outcome, e.primary_value, e.notes, e.created_at"
                " FROM benchbook.experiments e WHERE e.project_id=%s AND e.dedup_key=%s AND e.run_status='ok'"
                " AND NOT EXISTS (SELECT 1 FROM benchbook.experiments_valid v WHERE v.id = e.id)"
                " ORDER BY e.created_at DESC LIMIT 20", (self.project_id, key)).fetchall()
        note = None
        if inval:
            seeds = sorted({"none" if r["seed"] is None else str(r["seed"]) for r in inval})
            note = (f"{len(inval)} invalidated run(s) of this exact config and code_ref exist "
                    f"(seeds {', '.join(seeds)}); recording the same config, code_ref and seed again is rejected "
                    "as a duplicate. Change code_ref "
                    "(e.g. after fixing the evaluator) or use another seed.")
        return TriedResult(dedup_key=key, exact=[ExperimentSummary(**r) for r in rows],
                           replicates=int(agg["reps"]) if agg else 0,
                           best_value=agg["best_value"] if agg else None,
                           invalidated=[ExperimentSummary(**r) for r in inval], note=note)

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
        if run_status == "ok":
            problems += [f"metrics.{m}: required when run_status is ok (pack {self.pack.name})"
                         for m in self.pack.ok_metrics_required if m not in metrics]
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
        tid = self._rid(task_id, "task") if task_id is not None else self._only_lease()
        key = self._key(cfg, code_ref)
        pm, direction = self.project.primary_metric, self.project.metric_direction
        value = _num(metrics.get(pm)) if (pm and run_status == "ok") else None

        with self._conn() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.project_id,))
            if run_status == "ok":
                dup = conn.execute(
                    "SELECT id FROM benchbook.experiments WHERE project_id=%s AND dedup_key=%s"
                    " AND seed IS NOT DISTINCT FROM %s::bigint AND run_status='ok'",
                    (self.project_id, key, seed)).fetchone()
                if dup:
                    return self._reject(conn, key, dup["id"])
            if run_status != "ok":
                outcome = "failed"
            elif value is None:
                outcome = "unscored"
            else:
                # Settings are re-read: calibration (another process) may have changed the epsilon.
                prow = conn.execute("SELECT settings, improve_epsilon FROM benchbook.projects WHERE id=%s",
                                    (self.project_id,)).fetchone()
                pk = prow["settings"].get("partition_key")
                part = partition_text(cfg.get(pk)) if pk else None
                best = self._baseline(conn, prow["settings"], part)
                eps = partition_epsilon(prow["settings"], part, prow["improve_epsilon"])
                if best is None:
                    outcome = "improved"
                else:
                    better = value > best + eps if direction == "max" else value < best - eps
                    worse = value < best - eps if direction == "max" else value > best + eps
                    outcome = "improved" if better else ("worse" if worse else "no_change")
            try:
                with conn.transaction():
                    r = conn.execute(
                        """INSERT INTO benchbook.experiments(project_id,author,task_id,config,code_ref,dedup_key,seed,
                             run_status,metrics,primary_value,outcome,notes,artifacts,trace_ref,duration_ms,tokens,
                             cost_usd) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (self.project_id, self.agent, tid, Jsonb(cfg), code_ref, key, seed, run_status,
                         Jsonb(metrics), value, outcome, notes, Jsonb(arts),
                         Jsonb(trace_ref) if trace_ref is not None else None, duration_ms, tokens, cost_usd),
                    ).fetchone()
            except psycopg.errors.UniqueViolation:
                dup = conn.execute(
                    "SELECT id FROM benchbook.experiments WHERE project_id=%s AND dedup_key=%s"
                    " AND seed IS NOT DISTINCT FROM %s::bigint AND run_status='ok'",
                    (self.project_id, key, seed)).fetchone()
                return self._reject(conn, key, dup["id"] if dup else None)
        return RecordResult(id=r["id"], outcome=outcome, is_new_best=outcome == "improved",
                            duplicate_of=None, dedup_key=key, primary_value=value)

    def live_leases(self) -> list[Task]:
        """Tasks this agent holds a live lease on, intents excluded."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM benchbook.tasks WHERE project_id=%s AND status='claimed' AND lease_owner=%s"
                " AND lease_expires_at > now() AND NOT ('intent' = ANY(tags)) ORDER BY created_at",
                (self.project_id, self.agent)).fetchall()
        return [Task(**r) for r in rows]

    def _only_lease(self) -> UUID | None:
        """The task an experiment recorded without task_id belongs to: the one task this agent holds (U3)."""
        leases = self.live_leases()
        return leases[0].id if len(leases) == 1 else None

    def _baseline(self, conn: psycopg.Connection[Any], settings: dict[str, Any], part: str | None) -> float | None:
        """The value a new result is compared with: the best in its partition (C1), or with
        settings.outcome_baseline='verified' the best replicated mean when there is one."""
        if settings.get("outcome_baseline") == "verified":
            r = conn.execute("SELECT mean FROM benchbook.v_partition_best WHERE project_id=%s AND status='verified'"
                             " AND rank=1 AND partition IS NOT DISTINCT FROM %s", (self.project_id, part)).fetchone()
            if r is not None and r["mean"] is not None:
                return float(r["mean"])
        b = conn.execute("SELECT primary_value FROM benchbook.v_best WHERE project_id=%s"
                         " AND partition IS NOT DISTINCT FROM %s", (self.project_id, part)).fetchone()
        return None if b is None else float(b["primary_value"])

    def baseline(self, partition: str | None = None) -> tuple[float | None, float]:
        """(baseline value, epsilon) that record_experiment would use for this partition right now."""
        with self._conn() as conn:
            prow = conn.execute("SELECT settings, improve_epsilon FROM benchbook.projects WHERE id=%s",
                                (self.project_id,)).fetchone()
            return (self._baseline(conn, prow["settings"], partition),
                    partition_epsilon(prow["settings"], partition, prow["improve_epsilon"]))

    def _reject(self, conn: psycopg.Connection[Any], key: str, existing: UUID | None) -> RecordResult:
        conn.execute("INSERT INTO benchbook.dedup_hits(project_id,kind,dedup_key,existing_id,agent_id)"
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
                "INSERT INTO benchbook.claims(project_id,kind,statement,statement_hash,payload,tags,author,task_id)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id, statement_hash) DO NOTHING RETURNING id",
                (self.project_id, kind, statement, h, Jsonb(payload), list(tags or []), self.agent, tid),
            ).fetchone()
            dup = None
            if r is None:
                dup = conn.execute("SELECT id FROM benchbook.claims WHERE project_id=%s AND statement_hash=%s",
                                   (self.project_id, h)).fetchone()["id"]
            cid: UUID | None = r["id"] if r else dup
            assert cid is not None
            links = [self._link(conn, k, i, rel, "claim", cid, None) for (k, i), rel in resolved]
        return ClaimResult(id=cid, duplicate_of=dup, link_ids=links)

    def _link(self, conn: psycopg.Connection[Any], st: str, sid: UUID, rel: str, dt: str, did: UUID,
              note: str | None) -> UUID:
        if st not in ("claim", "experiment", "source", "task") or dt not in ("claim", "experiment", "source", "task"):
            raise BenchbookError("links connect claims, experiments, sources and tasks")
        if sid == did:
            raise BenchbookError("a record cannot link to itself")
        r = conn.execute(
            "INSERT INTO benchbook.links(project_id,src_type,src_id,relation,dst_type,dst_id,note,author)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (project_id, src_id, relation, dst_id) DO NOTHING"
            " RETURNING id", (self.project_id, st, sid, rel, dt, did, note, self.agent)).fetchone()
        if r is None:
            r = conn.execute("SELECT id FROM benchbook.links WHERE project_id=%s AND src_id=%s AND relation=%s"
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
            r = conn.execute("INSERT INTO benchbook.sources(project_id,uri,title,locator,sha256,author)"
                             " VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                             (self.project_id, uri, title, locator, sha256, self.agent)).fetchone()
        return r["id"]  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ decisions

    def claim_status(self, claim_id: str | UUID) -> str:
        cid = self._rid(claim_id, "claim")
        with self._conn() as conn:
            r = conn.execute("SELECT status FROM benchbook.v_claim_status WHERE id=%s", (cid,)).fetchone()
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
                "SELECT new_status, created_at FROM benchbook.decisions"
                " WHERE project_id=%s AND target_id=%s AND target_type=%s"
                " ORDER BY created_at DESC, id DESC LIMIT 1", (self.project_id, tid, tkind)).fetchone()
            if tkind == "claim":
                cur = last["new_status"] if last else "proposed"
                if new_status not in TRANSITIONS.get(cur, set()) and not force:
                    raise InvalidTransition(f"{cur} -> {new_status} is outside the claim state graph; pass force=True "
                                            "if you mean it")
                if new_status == "verified" and oracle == "rerun":
                    check = self._check_rerun(conn, tid, {eid for kind, eid in resolved if kind == "experiment"})
                    oracle_detail = {**(oracle_detail or {}), "rerun_check": check}
            r = conn.execute(
                "INSERT INTO benchbook.decisions(project_id,target_type,target_id,new_status,oracle,oracle_detail,"
                "rationale,evidence_ids,decided_by,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "greatest(clock_timestamp(), %s::timestamptz + interval '1 microsecond')) RETURNING *",
                (self.project_id, tkind, tid, new_status, oracle, Jsonb(oracle_detail or {}), rationale, ev,
                 self.agent, last["created_at"] if last else None)).fetchone()
        return Decision(**{k: r[k] for k in Decision.model_fields})

    def _check_rerun(self, conn: psycopg.Connection[Any], claim_id: UUID, experiment_ids: set[UUID]) -> dict[str, Any]:
        """Check the evidence mean and require independent reruns to also beat the baseline for an improvement."""
        prow = conn.execute("SELECT settings, improve_epsilon, primary_metric, metric_direction FROM benchbook.projects"
                            " WHERE id=%s", (self.project_id,)).fetchone()
        assert prow is not None
        st = prow["settings"]  # re-read: another process may have changed the thresholds
        runs = conn.execute(
            "SELECT id, dedup_key, author, seed, primary_value FROM benchbook.experiments_valid"
            " WHERE project_id=%s AND id=ANY(%s) AND run_status='ok'",
            (self.project_id, list(experiment_ids))).fetchall()
        if not runs or len(runs) != len(experiment_ids) or len({run["dedup_key"] for run in runs}) != 1:
            raise InvalidTransition("rerun oracle needs valid successful experiment evidence with the same config "
                                    "and code reference.")
        original = conn.execute(
            """WITH prior AS (
                  SELECT evidence_ids FROM benchbook.decisions
                  WHERE project_id=%(project)s AND target_id=%(claim)s
                    AND target_type='claim' AND new_status='supported'
                  ORDER BY created_at DESC, id DESC LIMIT 1
                ), cited AS (
                  SELECT e.id, e.dedup_key, e.author, e.created_at, e.config, e.code_ref FROM benchbook.experiments e
                  JOIN prior p ON e.id=ANY(p.evidence_ids)
                  WHERE e.project_id=%(project)s AND e.run_status='ok'
                ), original_candidates AS (
                  SELECT * FROM cited
                  UNION ALL
                  SELECT e.id, e.dedup_key, e.author, e.created_at, e.config, e.code_ref FROM benchbook.experiments e
                  JOIN benchbook.links l ON l.project_id=e.project_id AND l.src_id=e.id
                  WHERE e.project_id=%(project)s AND e.run_status='ok' AND l.src_type='experiment'
                    AND l.dst_type='claim' AND l.dst_id=%(claim)s AND l.relation='supports'
                    AND NOT EXISTS (SELECT 1 FROM cited)
                )
                SELECT id, author, created_at, config, code_ref FROM original_candidates WHERE dedup_key=%(key)s
                ORDER BY created_at, id LIMIT 1""",
            {"project": self.project_id, "claim": claim_id, "key": runs[0]["dedup_key"]}).fetchone()
        if original is None:
            raise InvalidTransition("rerun evidence must match the config and code reference of an original "
                                    "supporting experiment for the claim.")
        min_seeds, min_authors = int(st.get("rerun_min_seeds", 3)), int(st.get("rerun_min_authors", 2))
        seeds = {run["seed"] for run in runs if run["seed"] is not None}
        authors = {run["author"] for run in runs}
        independent = [run for run in runs if run["author"] != original["author"]]
        if len(seeds) < min_seeds or len(authors) < min_authors or not independent:
            raise InvalidTransition(
                f"rerun oracle needs >= {min_seeds} distinct seeds and >= {min_authors} distinct authors "
                f"including a rerun by someone other than {original['author']}; "
                f"got {len(seeds)} seeds, {len(authors)} authors. Create a replication task.")
        out: dict[str, Any] = {"n": len(runs), "seeds": len(seeds), "authors": sorted(authors),
                               "original": str(original["id"])}
        crow = conn.execute("SELECT kind, payload FROM benchbook.claims WHERE id=%s", (claim_id,)).fetchone()
        assert crow is not None
        payload: dict[str, Any] = crow["payload"]
        measurement = payload.get("measurement") is True
        result = crow["kind"] == "result"
        if prow["primary_metric"] is None:
            if measurement or result:
                raise InvalidTransition("a reproduced measurement needs a project primary metric")
            return out  # unscored project: nothing to compare
        if any(_num(r["primary_value"]) is None for r in runs):
            raise InvalidTransition(f"rerun oracle compares values: every evidence run needs "
                                    f"{prow['primary_metric']}")
        try:
            mean = fmean(r["primary_value"] for r in runs)
            rerun_mean = fmean(r["primary_value"] for r in independent)
        except OverflowError as e:
            raise InvalidTransition("experiment values are too large to compare") from e
        out.update(mean=mean, rerun_mean=rerun_mean)
        if measurement or result:
            stated_mean = _num(payload.get("mean"))
            if stated_mean is None or not math.isclose(stated_mean, mean, rel_tol=1e-12, abs_tol=1e-15):
                raise InvalidTransition(f"payload.mean must match the experiment evidence mean {mean:.12g}; "
                                        f"got {payload.get('mean')!r}")
        if result:
            partition = partition_text(original["config"].get(st.get("partition_key") or "partition"))
            origin = payload.get("origin")
            if (payload.get("partition") != partition or payload.get("code_ref") != original["code_ref"]
                    or (origin is not None and str(origin).lower() != str(original["id"]))):
                raise InvalidTransition("result payload partition, code_ref and origin must match the "
                                        "original experiment")
        if measurement:
            out["measurement"] = True
            return out
        pk = st.get("partition_key")
        part = partition_text(original["config"].get(pk)) if pk and isinstance(original["config"], dict) else None
        eps = max(partition_epsilon(st, part, prow["improve_epsilon"]), _num(payload.get("epsilon")) or 0.0)
        maximize = prow["metric_direction"] == "max"
        base, source = _num(payload.get("baseline")), "claim payload.baseline"
        if base is None:
            r = conn.execute(
                "SELECT primary_value FROM benchbook.experiments_valid WHERE project_id=%(p)s AND run_status='ok'"
                " AND primary_value IS NOT NULL AND dedup_key <> %(key)s AND created_at < %(t)s"
                " AND (%(pk)s::text IS NULL OR config ->> %(pk)s IS NOT DISTINCT FROM %(part)s)"
                " ORDER BY CASE WHEN %(max)s THEN -primary_value ELSE primary_value END LIMIT 1",
                {"p": self.project_id, "key": runs[0]["dedup_key"], "t": original["created_at"], "pk": pk,
                 "part": part, "max": maximize}).fetchone()
            base, source = (None if r is None else float(r["primary_value"])), "best earlier result"
        if base is None:
            raise InvalidTransition(
                "rerun oracle compares the reruns with a baseline and there is none: no earlier result with another "
                "config" + (f" in partition {part}" if part else "") + " and no numeric payload.baseline on the "
                "claim. Record a baseline run first, or set payload.measurement=true if the claim only states a "
                "reproduced value.")

        def beats(v: float) -> bool:
            return v > base + eps if maximize else v < base - eps

        out.update(baseline=base, baseline_source=source, epsilon=eps)
        if not beats(mean) or not beats(rerun_mean):
            raise InvalidTransition(
                f"the reruns do not reproduce the claim: mean {mean:.6g} over {len(runs)} runs, "
                f"independent reruns {rerun_mean:.6g}; both must beat "
                f"the baseline {base:.6g} ({source}) by epsilon {eps:g}. Decide refuted or inconclusive instead.")
        return out

    # ------------------------------------------------------------------ reads

    def get(self, ref: str | UUID) -> dict[str, Any]:
        kind, rid = self.resolve(ref)
        with self._conn() as conn:
            rec = conn.execute(psql.SQL("SELECT * FROM benchbook.{} WHERE id=%s").format(psql.Identifier(TABLE[kind])),
                               (rid,)).fetchone()
            out: dict[str, Any] = {"kind": kind, "id": short(kind, rid), "record": rec}
            if kind in ("claim", "experiment", "source", "task"):
                out["links_out"] = [
                    {"id": short("link", lk["id"]), "relation": lk["relation"],
                     "dst": short(lk["dst_type"], lk["dst_id"]), "note": lk["note"], "author": lk["author"]}
                    for lk in conn.execute("SELECT * FROM benchbook.links WHERE src_id=%s ORDER BY created_at", (rid,))]
                out["links_in"] = [
                    {"id": short("link", lk["id"]), "relation": lk["relation"],
                     "src": short(lk["src_type"], lk["src_id"]), "note": lk["note"], "author": lk["author"]}
                    for lk in conn.execute("SELECT * FROM benchbook.links WHERE dst_id=%s ORDER BY created_at", (rid,))]
            if kind in ("claim", "experiment"):
                out["decisions"] = [
                    {"id": short("decision", d["id"]), "status": d["new_status"], "oracle": d["oracle"],
                     "by": d["decided_by"], "rationale": d["rationale"], "at": d["created_at"]}
                    for d in conn.execute("SELECT * FROM benchbook.decisions WHERE target_id=%s ORDER BY created_at",
                                          (rid,))]
            if kind == "claim":
                out["status"] = conn.execute("SELECT * FROM benchbook.v_claim_status WHERE id=%s", (rid,)).fetchone()
            if kind == "experiment":
                out["invalidated"] = any(d["status"] == "invalidated" for d in out["decisions"])
            if kind == "task":
                out["events"] = conn.execute(
                    "SELECT event, agent_id, detail, created_at FROM benchbook.task_events"
                    " WHERE task_id=%s ORDER BY id",
                    (rid,)).fetchall()
        return jsonable(out)  # type: ignore[no-any-return]

    def status(self) -> dict[str, Any]:
        p = self.project_id
        with self._conn() as conn:
            best = conn.execute(
                "SELECT id, author, primary_value, created_at FROM benchbook.v_best WHERE project_id=%s ORDER BY"
                " CASE WHEN %s = 'min' THEN primary_value ELSE -primary_value END, created_at LIMIT 1",
                (p, self.project.metric_direction)).fetchone()
            partitions = conn.execute(
                "SELECT partition, primary_value AS best, id, author FROM benchbook.v_best WHERE project_id=%s"
                " AND partition IS NOT NULL ORDER BY partition", (p,)).fetchall()
            exp = conn.execute(
                "SELECT count(*) AS total, count(*) FILTER (WHERE run_status='ok') AS ok,"
                " count(*) FILTER (WHERE outcome='improved') AS improved FROM benchbook.experiments"
                " WHERE project_id=%s",
                (p,)).fetchone()
            inval = conn.execute("SELECT count(DISTINCT target_id) AS n FROM benchbook.decisions WHERE project_id=%s"
                                 " AND new_status='invalidated'", (p,)).fetchone()["n"]
            dd = conn.execute("SELECT rejected, avoided FROM benchbook.v_dedup_savings WHERE project_id=%s",
                              (p,)).fetchone()
            claims = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM benchbook.v_claim_status WHERE project_id=%s GROUP BY status", (p,))}
            tasks = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM benchbook.tasks WHERE project_id=%s GROUP BY status", (p,))}
            claimable = conn.execute("SELECT count(*) AS n FROM benchbook.v_open_tasks WHERE project_id=%s",
                                     (p,)).fetchone()["n"]
            active = [r["agent"] for r in conn.execute(
                "SELECT agent FROM benchbook.v_agent_stats WHERE project_id=%s"
                " AND last_active_at > now() - interval '15 minutes' AND role <> 'harness' ORDER BY agent", (p,))]
        pr = self.project
        out: dict[str, Any] = jsonable({
            "project": p, "goal": pr.goal, "metric": pr.primary_metric, "direction": pr.metric_direction,
            "best": None if best is None else {"id": short("experiment", best["id"]), "value": best["primary_value"],
                                               "author": best["author"], "at": best["created_at"]},
            "experiments": {**exp, "invalidated": inval},
            "dedup": dd, "claims": claims, "tasks": {**tasks, "claimable": claimable}, "active_agents": active,
        })
        if partitions:
            out["partitions"] = [{"partition": r["partition"], "best": r["best"], "id": short("experiment", r["id"]),
                                  "author": r["author"]} for r in partitions]
        return out

    def search(self, text: str | None = None, kind: str | None = None, status: str | None = None,
               tags: Sequence[str] | None = None, limit: int = 20,
               all_tags: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """One-line summaries ending with the record's tags (an experiment carries its task's tags).
        kind: claim|experiment|task or a claim kind (hypothesis, ...). status: claim status, task status or
        experiment outcome. tags: records with any of these tags; all_tags: records with every one of them."""
        limit = max(1, min(int(limit), 200))
        like = f"%{text}%" if text else None
        tg = list(tags or [])
        at = list(all_tags or [])
        claim_kind = kind if kind in self.pack.claim_kinds else None
        want = {"claim"} if claim_kind else ({kind} if kind else {"claim", "experiment", "task"})
        out: list[dict[str, Any]] = []
        with self._conn() as conn:
            if "claim" in want:
                for r in conn.execute(
                    "SELECT id, kind, status, statement, n_support, n_refute, tags, created_at"
                    " FROM benchbook.v_claim_status"
                    " WHERE project_id=%(p)s AND (%(l)s::text IS NULL OR statement ILIKE %(l)s)"
                    " AND (%(k)s::text IS NULL OR kind=%(k)s) AND (%(s)s::text IS NULL OR status=%(s)s)"
                    " AND (cardinality(%(t)s::text[])=0 OR tags && %(t)s::text[])"
                    " AND (cardinality(%(a)s::text[])=0 OR tags @> %(a)s::text[])"
                    " ORDER BY created_at DESC LIMIT %(n)s",
                    {"p": self.project_id, "l": like, "k": claim_kind, "s": status, "t": tg, "a": at, "n": limit}):
                    out.append({"id": short("claim", r["id"]), "kind": "claim", "at": r["created_at"],
                                "tags": list(r["tags"] or []),
                                "line": f"[{r['status']}] {r['kind']}: {r['statement']} "
                                        f"(+{r['n_support']}/-{r['n_refute']})" + _tag_suffix(r["tags"])})
            if "experiment" in want:
                for r in conn.execute(
                    "SELECT e.id, e.author, e.outcome, e.primary_value, e.config, e.notes, t.tags, e.created_at"
                    " FROM benchbook.experiments_valid e LEFT JOIN benchbook.tasks t ON t.id = e.task_id"
                    " WHERE e.project_id=%(p)s AND (%(l)s::text IS NULL OR e.notes ILIKE %(l)s"
                    "   OR e.config::text ILIKE %(l)s) AND (%(s)s::text IS NULL OR e.outcome=%(s)s)"
                    " AND (cardinality(%(t)s::text[])=0 OR t.tags && %(t)s::text[])"
                    " AND (cardinality(%(a)s::text[])=0 OR t.tags @> %(a)s::text[])"
                    " ORDER BY e.created_at DESC LIMIT %(n)s",
                    {"p": self.project_id, "l": like, "s": status, "t": tg, "a": at, "n": limit}):
                    val = "" if r["primary_value"] is None else f" {r['primary_value']:.6g}"
                    out.append({"id": short("experiment", r["id"]), "kind": "experiment", "at": r["created_at"],
                                "tags": list(r["tags"] or []),
                                "line": f"{r['outcome']}{val} by {r['author']}: {config_line(r['config'])}"
                                        + (f" | {r['notes'][:80]}" if r["notes"] else "") + _tag_suffix(r["tags"])})
            if "task" in want:
                for r in conn.execute(
                    "SELECT id, title, status, lease_owner, tags, created_at FROM benchbook.tasks"
                    " WHERE project_id=%(p)s"
                    " AND (%(l)s::text IS NULL OR title ILIKE %(l)s) AND (%(s)s::text IS NULL OR status=%(s)s)"
                    " AND (cardinality(%(t)s::text[])=0 OR tags && %(t)s::text[])"
                    " AND (cardinality(%(a)s::text[])=0 OR tags @> %(a)s::text[])"
                    " ORDER BY created_at DESC LIMIT %(n)s",
                    {"p": self.project_id, "l": like, "s": status, "t": tg, "a": at, "n": limit}):
                    out.append({"id": short("task", r["id"]), "kind": "task", "at": r["created_at"],
                                "tags": list(r["tags"] or []),
                                "line": f"[{r['status']}] {r['title']}"
                                        + (f" ({r['lease_owner']})" if r["status"] == "claimed" else "")
                                        + _tag_suffix(r["tags"])})
        out.sort(key=lambda x: x["at"], reverse=True)
        return [{k: v for k, v in jsonable(o).items() if k != "at"} for o in out[:limit]]

    def sql(self, query: str) -> dict[str, Any]:
        """Read-only SQL over the per-project v_* views (orchestrator tool, §7.3)."""
        q = query.strip().rstrip(";").strip()
        if not q:
            raise BenchbookError("empty query")
        if ";" in q:
            raise BenchbookError("one statement only (no ';')")
        if not re.match(r"^(select|with|table|values)\b", q, re.IGNORECASE):
            raise BenchbookError("only SELECT queries over v_* views are allowed")
        if _FORBIDDEN_SQL.search(q):
            raise BenchbookError("query uses a forbidden function or command")
        conn, via_set_role = self._reader_conn()
        try:
            with conn.transaction():
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute("SET LOCAL statement_timeout = '2s'")
                conn.execute("SET LOCAL search_path = benchbook_q")
                # SET, not set_config(): migration 0006 revokes EXECUTE on set_config from the reader.
                conn.execute(psql.SQL("SET LOCAL benchbook.project = {}").format(psql.Literal(self.project_id)))
                if via_set_role:
                    conn.execute("SET LOCAL ROLE benchbook_reader")
                guard = conn.execute("SELECT current_user AS u, current_setting('benchbook.project') AS p").fetchone()
                try:
                    # Describe the original query without fetching any rows. Experiments can contain
                    # configurations larger than the entire SQL budget.
                    with conn.cursor(name="benchbook_columns") as columns:
                        columns.execute(q.encode("utf-8"))
                        cols = [c.name for c in (columns.description or [])]
                    bounded_query = psql.SQL(
                        "SELECT CASE WHEN octet_length(convert_to(row_data, 'UTF8')) <= {}"
                        " THEN row_data END AS row_data"
                        " FROM (SELECT row_to_json(benchbook_result.*)::text AS row_data"
                        " FROM (\n{}\n) AS benchbook_result) AS benchbook_serialized"
                    ).format(psql.Literal(SQL_TOKEN_LIMIT * 4), psql.SQL(q))
                    with conn.cursor(name="benchbook_rows", scrollable=False) as cursor:
                        cursor.execute(bounded_query)
                        fetched = cursor.fetchmany(SQL_ROW_LIMIT)
                        remaining = conn.execute(
                            psql.SQL("MOVE FORWARD ALL FROM {}").format(psql.Identifier(cursor.name))
                        ).rowcount
                    # Defence in depth (C4): if the query changed the project filter or the role, discard it.
                    after = conn.execute(
                        "SELECT current_user AS u, current_setting('benchbook.project') AS p").fetchone()
                    if after != guard:
                        raise BenchbookError("query changed the session's project or role; refused")
                except psycopg.errors.QueryCanceled as e:
                    raise BenchbookError("query exceeded the 2s statement timeout") from e
                except psycopg.errors.InsufficientPrivilege as e:
                    raise BenchbookError(
                        f"permission denied: only the v_* views are readable ({e.diag.message_primary})"
                    ) from e
                except psycopg.Error as e:
                    raise BenchbookError(f"SQL error: {e.diag.message_primary or e}") from e
        finally:
            conn.close()
        total = len(fetched) + remaining
        rows: list[dict[str, Any]] = []
        response: dict[str, Any] = {"columns": cols, "rows": rows, "row_count": total, "returned": 0,
                                    "truncated": total > 0}
        if estimate_tokens(json.dumps(response, ensure_ascii=False)) > SQL_TOKEN_LIMIT:
            raise BenchbookError("SQL column metadata exceeds the 4,000-token limit; select fewer columns")
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
        """Connect as benchbook_reader so the session user itself is the reader. If that is impossible
        (a managed Postgres with password auth and no reader password), fall back to SET ROLE."""
        import os

        kw: dict[str, Any] = {"row_factory": dict_row, "autocommit": False, "user": "benchbook_reader"}
        if pw := os.environ.get("BENCHBOOK_READER_PASSWORD"):
            kw["password"] = pw
        try:
            return psycopg.connect(self.url, **kw), False
        except psycopg.OperationalError as e:
            # SET ROLE keeps the owner as session user, so a query that escapes the reader role regains its
            # privileges. Only on explicit opt-in (C4).
            if os.environ.get("BENCHBOOK_SQL_ALLOW_SET_ROLE") != "1":
                raise BenchbookError(
                    "benchbook_sql connects as the benchbook_reader role and could not log in "
                    f"({str(e).strip().splitlines()[0] if str(e).strip() else 'connection failed'}). Set a password "
                    "for it (ALTER ROLE benchbook_reader PASSWORD '...') and export BENCHBOOK_READER_PASSWORD, or set "
                    "BENCHBOOK_SQL_ALLOW_SET_ROLE=1 to accept the weaker SET ROLE fallback.") from e
            return psycopg.connect(self.url, row_factory=dict_row, autocommit=False), True

    def brief(self, task_id: str | UUID | None = None, budget_tokens: int = 2000,
              partition: str | None = None) -> str:
        from .brief import compile_brief

        tid = self._rid(task_id, "task")
        return compile_brief(self, tid, budget_tokens, partition=partition)

    def export(self) -> Iterator[dict[str, Any]]:
        """The whole project as an append-only event stream, oldest first."""
        tables = ["agents", "tasks", "task_events", "sources", "experiments", "claims", "links", "decisions",
                  "dedup_hits", "brief_log", "settings_events", "blobs"]
        with self._conn() as conn:
            proj = conn.execute("SELECT * FROM benchbook.projects WHERE id=%s", (self.project_id,)).fetchone()
            yield {"type": "project", "record": jsonable(proj)}
            events: list[tuple[Any, str, dict[str, Any]]] = []
            for t in tables:
                for r in conn.execute(psql.SQL("SELECT * FROM benchbook.{} WHERE project_id=%s").format(
                        psql.Identifier(t)), (self.project_id,)):
                    events.append((r["created_at"], t, r))
        events.sort(key=lambda e: (e[0], tables.index(e[1])))
        for _, t, r in events:
            plural = ("dedup_hits", "brief_log", "settings_events")
            yield {"type": t.rstrip("s") if t not in plural else t, "record": jsonable(r)}

    def sql_internal(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        """Full-privilege read for adapters (viewer). Not exposed to agents."""
        with self._conn() as conn:
            return conn.execute(query, params).fetchall()  # type: ignore[no-any-return]


BLOB_PREFIX = "benchbook://blob/"


def partition_text(v: Any) -> str | None:
    """The text Postgres `config ->> key` yields for a config value."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, separators=(", ", ": ")) if isinstance(v, (dict, list)) else json.dumps(v)


def partition_epsilon(settings: dict[str, Any], part: str | None, default: float) -> float:
    p = (settings.get("partitions") or {}).get(part) if part is not None else None
    if isinstance(p, dict) and _num(p.get("epsilon")) is not None:
        return float(p["epsilon"])
    return float(default)


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch: objects merge recursively, null deletes, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def _tag_suffix(tags: Sequence[str] | None) -> str:
    return f" [tags: {', '.join(tags)}]" if tags else ""


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
