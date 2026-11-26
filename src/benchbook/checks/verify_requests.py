"""Resolve a Claude Code report in the hook and verify it in the existing spool processor."""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .. import config
from ..code.pr import TRAILER
from ..hooks import boundary as boundary_hooks
from ..hooks import citations, merge_gate, session_notes, spooler
from ..hooks import subagent as hooks
from .run import PIECE

if TYPE_CHECKING:
    from .verify import Recorded, Verification

_DONE = re.compile(r"(?m)^\s*DONE[ \t]+([0-9a-fA-F]{7,40})[ \t]*$")
_COMMIT = re.compile(r"(?im)^[ \t]*(?:commit|커밋):[ \t]*([0-9a-f]{7,40})[ \t]*$")
_SHA = re.compile(r"\b[0-9a-fA-F]{40}\b")
_PIECE = re.compile(r"(?im)^[ \t]*piece:[ \t]*(\S+)[ \t]*$")


def reported_ref(report: str) -> str | None:
    lines = report.rstrip().splitlines()
    if lines and (done := _DONE.fullmatch(lines[-1])):
        return done[1].lower()
    for pattern in (_COMMIT, _SHA):
        refs = {m.lower() for m in pattern.findall(report)}
        if refs:
            return next(iter(refs)) if len(refs) == 1 else None
    return None


def reported_piece(repo: Path, commit: str, report: str) -> str | None:
    git = boundary_hooks._git
    trailers = git(repo, "show", "-s", f"--format=%(trailers:key={TRAILER},valueonly)", commit)
    branches = git(repo, "for-each-ref", "--points-at", commit, "--format=%(refname:lstrip=2)", "refs/heads") \
        if not trailers and not _PIECE.search(report) else ""
    for values in (trailers.splitlines(), _PIECE.findall(report), branches.splitlines()):
        names = set(values)
        if names:
            name = next(iter(names))
            return name if len(names) == 1 and PIECE.fullmatch(name) and name.isprintable() else None
    return None


def reported_target(repo: Path, report: str) -> tuple[str, str] | None:
    """(commit, piece) a report names in repo: its commit (reported_ref) resolved to a full sha, and the piece
    (reported_piece). The automatic re-run and the out-of-scope findings of the same report both use it, so the
    findings are tagged with the piece the re-run verifies (PR #4 issue 3)."""
    ref = reported_ref(report)
    if ref is None:
        return None
    commit = boundary_hooks._git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if not _SHA.fullmatch(commit) or (piece := reported_piece(repo, commit, report)) is None:
        return None
    return commit, piece


def enqueue(data: dict[str, Any]) -> str:
    """Queue a re-run for a report that names a commit. The report is the foreground Agent result (PostToolUse) or,
    for an agent Claude Code ran in the background, its SubagentStop (agent_type and last_assistant_message): since
    2.x an Agent call without run_in_background comes back async_launched too (T08, 2.1.330)."""
    response = data.get("tool_response")
    if data.get("hook_event_name") == "SubagentStop":
        if not data.get("agent_type"):  # compaction runs as an untyped subagent
            return ""
    elif data.get("tool_name") not in ("Agent", "Task") or (
            isinstance(response, dict) and response.get("status") == "async_launched"):
        return ""
    session = data.get("session_id", "")
    report = citations.extract_text(data)
    if reported_ref(report) is None or session_notes.notes_dir(session) is None:
        return ""
    where = boundary_hooks.where(data)
    if where is None or where.repo is None or (target := reported_target(where.repo, report)) is None:
        return ""
    commit, piece = target
    reporter = hooks._agent_type(data) or "unknown"
    if len(reporter) > 100 or not reporter.isprintable():
        return ""
    line = f"benchbook: re-running the checks of {piece} at {commit[:7]} in the background"
    notes = session_notes.notes_dir(session)
    assert notes is not None  # checked above
    notes.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps([where.project, piece, commit]).encode()).hexdigest()
    name = f"queued-{key}.txt"
    queued, said = notes / name, notes / session_notes.DELIVERED / name
    stop = data.get("hook_event_name") == "SubagentStop"
    spool = None
    # Keep one display notice per commit, but only suppress a foreground request if SubagentStop queued this
    # report for this writer. Another report of the same commit may need a fresh run after an environment failure.
    report_hash = hashlib.sha256(report.encode()).hexdigest()
    try:
        with spooler._lock(notes, blocking=True):
            target_spool = spooler._spool_dir()
            proof_key = hashlib.sha256(json.dumps([name, target_spool.name, reporter, report.strip()]).encode())
            proof = notes / f"queued-{proof_key.hexdigest()}.request"
            if stop or not proof.is_file() or not target_spool.is_dir():
                target_spool.mkdir(parents=True, exist_ok=True, mode=0o700)
                request = {"piece": piece, "commit": commit, "agent_type": reporter, "session_id": session,
                           "repo": str(where.repo), "report_sha256": report_hash}
                spooler._write_spool(target_spool, {"event": "verify-request", "project": where.project,
                                                    "accepted": True, "data": request})
                spool = target_spool
                # A report-specific environment verdict belongs to this accepted request only.
                report_key = hashlib.sha256(json.dumps([key, session, reporter, report_hash]).encode()).hexdigest()
                merge_gate.mark(notes, commit, piece, [f"verify-{key}.txt", f"verify-{report_key}.txt"], reporter)
            else:
                spool = target_spool  # recover if the paired hook exited before launching the processor

            if stop:
                session_notes.write_note(proof, "queued")
                if not queued.exists() and not said.exists():
                    session_notes.write_note(queued, line)
                return ""

            proof.unlink(missing_ok=True)
            if queued.exists():
                said.parent.mkdir(exist_ok=True)
                with suppress(FileNotFoundError):  # another lead hook may have delivered it
                    os.replace(queued, said)
                    return line
                return ""
            if not said.exists():
                said.parent.mkdir(exist_ok=True)
                session_notes.write_note(said, line)
                return line
            return ""
    finally:
        if spool is not None:  # a note write can fail after the request has already been queued
            hooks._log_notices()
            spooler.start_processor(spool, spooler.VERIFY)


def _result_line(run: Verification, record: Recorded, oracle: str | None = None, role: str | None = None) -> str:
    """role: the re-running agent's; when nothing decided the claim, the line says why the rest of it does not."""
    from ..core.ids import short
    from ..core.ledger import DECIDERS
    from ..core.lookup import checked
    from .verify import RETRY_FAILED, RETRY_FAILED_OTHER, RETRY_PASSED

    if run.environment:
        result = "environment failure"
    elif run.retry == RETRY_PASSED:  # not "re-run passed", which clears a merge
        result = f"re-run failed ({(run.first_signature or 'a check')[:120]}), then passed on a retry"
    elif run.retry in (RETRY_FAILED, RETRY_FAILED_OTHER):
        result = "re-run failed, and again on a retry" + (
            " with a different failure" if run.retry == RETRY_FAILED_OTHER else "")
    else:
        result = "re-run passed" if run.all_passed and run.complete else "re-run failed"
    guard = ("tests weakened or check declarations changed; needs review"
             if run.integrity.guard_weakened or run.integrity.checks_changed else "tests not weakened")
    verdict = checked(oracle)
    if record.outcome and record.outcome.decision == "inconclusive":
        guard += "; attempts disagree"
    elif record.outcome and record.outcome.person and "needs review" not in guard:
        guard += "; needs review"
    if oracle is None and record.claim_id is not None and result == "re-run passed" and "needs review" not in guard:
        if role is not None and role not in DECIDERS:
            verdict += f" (recorded by a {role} agent; a lead decides)"
        elif not run.complete:
            verdict += " (not every declared check ran)"
    ref = short("claim", record.claim_id) if record.claim_id else short("experiment", record.experiment_id)
    if run.retry_of is not None:  # the gate's hold message names the runs to invalidate
        ref += (f"; failed run {short('experiment', run.retry_of)}" if run.retry == RETRY_PASSED else
                f"; runs {short('experiment', run.retry_of)}, {short('experiment', record.experiment_id)}")
    return (f"benchbook: {run.piece} {run.commit[:7]}: {result} ({run.total} checks, "
            f"{run.duration_ms / 1000:.1f}s), {guard}, checked: {verdict} ({ref})")


def _cached_line(path: Path) -> str | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    if isinstance(cached, dict) and isinstance(line := cached.get("line"), str):
        return line
    return None


def process(data: dict[str, Any], project: str, receipt: UUID, spool: Path) -> str:
    """The spool lock serializes attempts for this database and writer. Keep the result for duplicate reports."""
    from ..core.ledger import Ledger
    from . import verify as checks

    piece, commit, reporter = data["piece"], data["commit"], data["agent_type"]
    notes = session_notes.notes_dir(data["session_id"])
    if notes is None or not PIECE.fullmatch(piece) or not _SHA.fullmatch(commit):
        raise ValueError("invalid verification request")
    # Authenticate even for a cached result: a token may have been revoked since it queued this request.
    lg = Ledger.open(project=project, agent=config.default_agent(checks.default_rerunner()), role="orchestrator")
    if lg.token is not None:
        spooler.remember_writer(project, lg.agent)
    if lg.pack.name != "code":
        return "skipped:requires code pack"
    key = hashlib.sha256(json.dumps([project, piece, commit]).encode()).hexdigest()
    results = spool / "verify-results"
    result = results / f"{key}.json"
    report_identity = [key, data["session_id"], reporter, data["report_sha256"]]
    report_key = hashlib.sha256(json.dumps(report_identity).encode()).hexdigest()
    transient = results / f"report-{report_key}.json"
    line = _cached_line(result)
    # Older processors wrote environment failures into the commit cache. Only reuse them for the original report
    # when that report has its own cache entry.
    if line is not None and line.startswith(f"benchbook: {piece} {commit[:7]}: environment failure ("):
        line = None
    note_key = key
    if line is None:
        line = _cached_line(transient)
        if line is not None:
            note_key = report_key
    if line is None:
        run, record = checks.verify(lg, piece, commit, repo=Path(data["repo"]), reported_by=reporter,
                                    trace_ref={"source": "agent-result", "report_sha256": data["report_sha256"]},
                                    receipt_id=receipt, retry=True)
        oracle = None
        if record.claim_id is not None:
            rows = lg.sql_internal(
                "SELECT last_oracle FROM benchbook.v_claim_status WHERE project_id = %s AND id = %s"
                " AND status = 'verified' AND NOT evidence_invalidated", (project, record.claim_id))
            if rows:
                oracle = rows[0]["last_oracle"]
        line = _result_line(run, record, oracle, lg.role)
        results.mkdir(mode=0o700, exist_ok=True)
        if run.environment is not None:
            note_key = report_key
            result = transient
        spooler._write_info(result, {"line": line})
    notes.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"verify-{note_key}.txt"
    with spooler._lock(notes, blocking=True):
        if not (notes / name).exists() and not (notes / session_notes.DELIVERED / name).exists():
            session_notes.write_note(notes / name, line)
    return "recorded:verification"
