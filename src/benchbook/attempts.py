"""Check attempts that reach the ledger through the hook spool: `benchbook check run` (the wrapper) and the
Bash hook (`benchbook hook bash-result`) queue them, the spool's processor records them here, and the completion gate
(`benchbook hook task-completed`) reads them back.

An attempt has the shape `check verify` writes (checks.Verification: config piece and checks, code_ref the commit, the
same metrics, recorded through checks._record) plus two metrics that say what it is not:

- `via`: "check-run" or "bash-hook". These are the implementer's own runs in its own checkout, never an independent
  re-run, whoever the author turns out to be.
- `worktree_dirty`: the checkout had uncommitted (or untracked) paths, so the run tested something that is not the
  commit in code_ref. The gate does not count such a run.

guard_touched, guard_files and checks_changed are computed here, at replay, against the merge-base of the guard base
and the commit (as check verify does). When they cannot be (the base ref is missing, no declarations at the
merge-base, the checkout is gone) they are left out and the notes say why; a missing guard_touched is not "false".
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from . import checks
from . import config as localcfg
from .checks import Base, CheckResult, Integrity, Verification
from .errors import BenchbookError

GUARD_KEYS = ("guard_touched", "guard_files", "checks_changed", "guard_weakened", "guard_weakened_files",
              "guard_added_files", "tests_before", "tests_after", "fewer_tests_checks")
STATUSES = {"ok": "ok", "fail": "fail", "timeout": "timeout", "interrupted": "not_run"}
SOURCES = {"check-run": "check run", "bash-result": "Bash hook"}
CONNECT_TIMEOUT = 5  # seconds; the gate fails open after that
STATEMENT_TIMEOUT_MS = 5000


@dataclass
class LocalRun(Verification):
    """One check run in the implementer's checkout, recorded in check verify's shape."""
    via: str = "check-run"
    dirty: bool = False
    changed: int = 0
    integrity_note: str | None = None  # why the guard columns are missing; None when they were computed
    source_note: str = ""
    # What check verify needs to count this run as the implementer's own pass (checks.passed_elsewhere): the hash of
    # the definition that ran (CheckSpec.definition) and, for the Bash hook, the agent type that ran it.
    definition: str | None = None
    run_by: str | None = None

    def metrics(self) -> dict[str, Any]:
        m = super().metrics()
        if self.integrity_note is not None:
            for k in GUARD_KEYS:
                m.pop(k, None)
        m["via"] = self.via
        m["worktree_dirty"] = self.dirty
        if self.definition:
            m["check_definitions"] = {r.name: self.definition for r in self.results}
        if self.run_by:
            m["run_by"] = self.run_by
        return m

    def notes(self) -> str:
        n = self.source_note
        if self.dirty:
            n += f"; {self.changed} uncommitted path(s) in the checkout: this run is not a run of {self.commit[:12]}"
        if self.integrity_note is not None:
            n += f"; guard not computed: {self.integrity_note}"
        else:
            n += f"; guard against {self.base.ref} (merge-base {self.base.merge_base[:12]})"
        if self.interrupted:
            n += f"; interrupted ({self.interrupted})"
        return n[:2000]


def _integrity(repo: Path, sha: str) -> tuple[Base, Integrity] | str:
    try:
        if not repo.is_dir():
            return f"{repo} no longer exists"
        b = checks.read_base(repo, localcfg.load(repo).guard_base or checks.DEFAULT_BASE, sha)
        return b, checks.integrity(repo, b, sha)
    except (BenchbookError, OSError, ValueError) as e:
        return str(e).splitlines()[0] if str(e) else type(e).__name__


def build(event: str, data: dict[str, Any]) -> LocalRun:
    """The attempt a spool entry describes. KeyError/TypeError/ValueError on a malformed entry (the spool moves it to
    dead/)."""
    name, status, sha = str(data["check"]), STATUSES[str(data["status"])], str(data["sha"])
    failed = [(str(t[0]), None if t[1] is None else str(t[1])) for t in data.get("failed_tests") or []]
    part = data.get("signature")  # check run signs where the output was; the Bash hook leaves it to check_part
    result = CheckResult(name, status, data.get("exit_code"), int(data.get("duration_ms") or 0), failed,
                         data.get("last_line_hash"), signature=part if isinstance(part, str) and part else None)
    found = _integrity(Path(str(data["repo"])), sha)
    paths: list[str] = []
    if isinstance(found, str):
        base, integ, note = Base("", "", "", "", {}, [], False), Integrity([], False), found
    else:
        (base, integ), note = found, None
        try:  # T06: the directories the commit changed, as check verify records them
            paths = checks.path_prefixes(checks.changed_paths(Path(str(data["repo"])), base.merge_base, sha))
        except BenchbookError:
            paths = []
    source = f"{SOURCES[event]}: {name} in {data['repo']}"
    if event == "bash-result":
        ids = {k: data.get(k) for k in ("harness", "session_id", "tool_use_id", "agent_type") if data.get(k)}
        if ids:
            source += " (" + ", ".join(f"{k} {v}" for k, v in ids.items()) + ")"
    approach = str(data["approach"]) if data.get("approach") else None
    definition = data.get("definition")
    run_by = data.get("agent") or data.get("agent_type")
    return LocalRun(str(data["piece"]), sha, base, integ, [result], result.duration_ms, data.get("interrupted"),
                    approach, declared=list(base.checks),
                    via=str(data.get("via") or ("bash-hook" if event == "bash-result" else "check-run")),
                    paths=paths, dirty=bool(data.get("dirty")), changed=int(data.get("changed") or 0),
                    integrity_note=note, source_note=source,
                    definition=definition if isinstance(definition, str) and definition else None,
                    run_by=str(run_by)[:100] if run_by else None)


def record_spooled(event: str, data: dict[str, Any], project: str, receipt_id: UUID) -> str:
    """The spool processor's step for an attempt entry: 'recorded:e:<id>'. Raises like any ledger write; the spool
    keeps transient failures queued and moves permanent ones to dead/."""
    from .config import default_agent
    from .ids import short
    from .ledger import Ledger

    run = build(event, data)
    agent = data.get("agent") or None
    # Without a name the attempt is the harness's (the hook's), or under BENCHBOOK_TOKEN the token's agent.
    lg = Ledger.open(project=project, agent=agent or default_agent("hook"), role=None if agent else "harness")
    if lg.token is not None:
        from .hooks import remember_writer

        remember_writer(lg.project_id, lg.agent)
    if "completion" not in lg.pack.claim_kinds:
        raise BenchbookError(f"check attempts need the code pack (benchbook init --pack code); {project} uses "
                             f"{lg.pack.name}")
    rec = checks._record(lg, run, None, None, receipt_id=receipt_id)
    out = f"recorded:{short('experiment', rec.experiment_id)}"
    if rec.discarded_id is not None:
        out += f" discarded:{short('claim', rec.discarded_id)}{'' if rec.discarded_new else ' (seen before)'}"
    elif rec.discarded_error:
        out += f" (discarded-approach claim not recorded: {rec.discarded_error})"
    return out


# ---------------------------------------------------------------------------------------------- the gate


def live_piece(project: str, piece: str) -> bool:
    """Whether piece has a live `piece start` intent (v_inflight: claimed and its lease not expired). The gate's
    test for "this branch is a piece" when only the branch name says so. A plain connection, as check_states."""
    import psycopg

    from .db.conn import database_url

    with psycopg.connect(database_url(), connect_timeout=CONNECT_TIMEOUT, autocommit=True) as conn:
        conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        row = conn.execute("SELECT 1 FROM benchbook.v_inflight WHERE project_id = %s AND is_intent"
                           " AND 'piece' = ANY(tags) AND spec ->> 'piece' = %s LIMIT 1", (project, piece)).fetchone()
    return row is not None


def check_states(project: str, piece: str, sha: str,
                 junit_checks: Collection[str] = ()) -> tuple[str | None, dict[str, bool]]:
    """(the project's pack, or None when the project does not exist; {check name: passed}) over the valid,
    finished attempts of piece at sha whose checkout was clean. A check is passed when at least one such attempt
    passed it. Raw Bash records made by older versions cannot identify which run produced a JUnit report.
    Read with a plain connection (no migration, no agent registration): the gate only reads, and must
    answer within the hook's timeout or fail open."""
    import psycopg

    from .db.conn import database_url

    with psycopg.connect(database_url(), connect_timeout=CONNECT_TIMEOUT, autocommit=True) as conn:
        conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        row = conn.execute("SELECT pack FROM benchbook.projects WHERE id = %s", (project,)).fetchone()
        if row is None:
            return None, {}
        rows = conn.execute(
            """SELECT c.key, bool_or(c.value = 'ok') AS passed
               FROM benchbook.experiments_valid e,
                    jsonb_each_text(CASE WHEN jsonb_typeof(e.metrics -> 'checks') = 'object'
                                         THEN e.metrics -> 'checks' ELSE '{}'::jsonb END) AS c(key, value)
               WHERE e.project_id = %s AND e.config ->> 'piece' = %s AND e.code_ref = %s AND e.run_status = 'ok'
                 AND e.metrics -> 'worktree_dirty' IS DISTINCT FROM 'true'::jsonb
                 AND (c.value <> 'ok' OR c.key <> ALL(%s::text[])
                      OR e.metrics ->> 'via' IS DISTINCT FROM 'bash-hook')
               GROUP BY c.key""", (project, piece, sha, list(junit_checks))).fetchall()
    return str(row[0]), {str(k): bool(v) for k, v in rows}
