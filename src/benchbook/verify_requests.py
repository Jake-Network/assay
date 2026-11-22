"""Resolve a Claude Code report in the hook and verify it in the existing spool processor."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from . import boundary_hooks, config, hooks, session_notes
from .check_run import PIECE
from .pr import TRAILER

if TYPE_CHECKING:
    from .checks import Recorded, Verification

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


def enqueue(data: dict[str, Any]) -> str:
    response = data.get("tool_response")
    if data.get("tool_name") not in ("Agent", "Task") or (
            isinstance(response, dict) and response.get("status") == "async_launched"):
        return ""
    session = data.get("session_id", "")
    report = hooks.extract_text(data)
    ref = reported_ref(report)
    if ref is None or session_notes.notes_dir(session) is None:
        return ""
    where = boundary_hooks.where(data)
    if where is None or where.repo is None:
        return ""
    commit = boundary_hooks._git(where.repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    if not _SHA.fullmatch(commit) or (piece := reported_piece(where.repo, commit, report)) is None:
        return ""
    reporter = hooks._agent_type(data) or "unknown"
    if len(reporter) > 100 or not reporter.isprintable():
        return ""
    spool = hooks._spool_dir()
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    request = {"piece": piece, "commit": commit, "agent_type": reporter, "session_id": session,
               "repo": str(where.repo), "report_sha256": hashlib.sha256(report.encode()).hexdigest()}
    hooks._write_spool(spool, {"event": "verify-request", "project": where.project, "accepted": True, "data": request})
    hooks._log_notices()
    hooks.start_processor(spool)
    return f"benchbook: re-running the checks of {piece} at {commit[:7]} in the background"


def _result_line(run: Verification, record: Recorded) -> str:
    from .ids import short

    if run.environment:
        result = "environment failure"
    else:
        result = "re-run passed" if run.all_passed and run.complete else "re-run failed"
    guard = ("tests weakened or check declarations changed; needs review"
             if run.integrity.guard_weakened or run.integrity.checks_changed else "tests not weakened")
    verdict = "re-run" if record.outcome and record.outcome.decision == "verified" else "none"
    if record.outcome and record.outcome.decision == "inconclusive":
        guard += "; attempts disagree"
    elif record.outcome and record.outcome.person and "needs review" not in guard:
        guard += "; needs review"
    ref = short("claim", record.claim_id) if record.claim_id else short("experiment", record.experiment_id)
    return (f"benchbook: {run.piece} {run.commit[:7]}: {result} ({run.total} checks, "
            f"{run.duration_ms / 1000:.1f}s), {guard}, checked: {verdict} ({ref})")


def process(data: dict[str, Any], project: str, receipt: UUID, spool: Path) -> str:
    """The spool lock serializes attempts for this database and writer. Keep the result for duplicate reports."""
    from . import checks
    from .ledger import Ledger

    piece, commit, reporter = data["piece"], data["commit"], data["agent_type"]
    notes = session_notes.notes_dir(data["session_id"])
    if notes is None or not PIECE.fullmatch(piece) or not _SHA.fullmatch(commit):
        raise ValueError("invalid verification request")
    # Authenticate even for a cached result: a token may have been revoked since it queued this request.
    lg = Ledger.open(project=project, agent=config.default_agent(checks.default_verifier()), role="orchestrator")
    if lg.token is not None:
        hooks.remember_writer(project, lg.agent)
    if lg.pack.name != "code":
        return "skipped:requires code pack"
    key = hashlib.sha256(json.dumps([project, piece, commit]).encode()).hexdigest()
    results = spool / "verify-results"
    result = results / f"{key}.json"
    try:
        line = json.loads(result.read_text(encoding="utf-8"))["line"]
    except FileNotFoundError:
        run, record = checks.verify(lg, piece, commit, repo=Path(data["repo"]), reported_by=reporter,
                                    trace_ref={"source": "agent-result", "report_sha256": data["report_sha256"]},
                                    receipt_id=receipt)
        line = _result_line(run, record)
        results.mkdir(mode=0o700, exist_ok=True)
        hooks._write_info(result, {"line": line})
    notes.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"verify-{key}.txt"
    with hooks._lock(notes, blocking=True):
        if not (notes / name).exists() and not (notes / session_notes.DELIVERED / name).exists():
            session_notes.write_note(notes / name, line)
    return "recorded:verification"
