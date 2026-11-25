"""Claude Code hook adapter (§12.3): make sure a subagent's result reaches the ledger.

`benchbook hook subagent-stop` reads the hook input (JSON on stdin), spools it to a file and starts a detached
processor (unless one is already draining that spool), so the hook itself returns within milliseconds and always
exits 0. The processor extracts the subagent's final text; unless it cites an experiment or claim id this run
recorded, it records an auto-captured note. Task ids (t:) never count as a citation: naming the task you worked
on is not recording its result.

Inputs handled:
- SubagentStop: last_assistant_message
- PostToolUse on Agent/Task: tool_response (text blocks)
- PostToolUse on SubagentHandback: tool_input.message (the report, when handed back through that tool)

Only benchbook workers are captured: the agent type must be one of BENCHBOOK_HOOK_AGENTS (default
`benchbook-worker` and its pre-rename name `assay-worker`, matched with or without a plugin prefix such as
`benchbook:`). The plugin's matcher passes every subagent; any other type, and an event with no agent type at all,
exits before the config is read or anything is spooled (`BENCHBOOK_HOOK_AGENTS=*` captures every event). A run that
handed its report back is captured once, from the handback. Permanent failures (unknown project, schema newer than
this benchbook, data Postgres refuses) move to `dead/` in the spool; `benchbook hook replay` retries them once more
and reports what it did.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import config
from ..config import env
from . import spooler
from .citations import _cites_own_record, _run_receipts, extract_text

MAX_INPUT = 2_000_000
STATEMENT_HEAD = 300
PAYLOAD_TEXT = 4000
MARKER_TTL = 86400
# the worker subagent of the plugin, under its current name and the one it had before the rename
WORKER_TYPES = frozenset({"benchbook-worker", "assay-worker"})
# Domain separator of the auto-capture dedup hash. It is stored with every captured note, so it keeps the old name:
# a report captured before the upgrade and replayed after it must hash to the same claim.
CAPTURE_DOMAIN = b"assay:auto-captured\0"


def _log_notices() -> None:
    """Write the notes held back from stderr (config.hold_notices) to hook.log."""
    for note in config.take_notices():
        spooler.log(f"note: {note}")


def main(event: str, harness: str | None = None) -> spooler.ReplaySummary | None:
    """Spool atomically and detach without waiting for Postgres. `replay` drains in the foreground instead and
    returns a summary (the caller prints it). harness: `--harness codex|cursor` from that harness's manifest; its
    subagent-stop input is read into Claude Code's shape first (harnesses.report_input)."""
    try:
        return _main(event, harness)
    finally:
        if harness and event != "replay":
            from .harnesses import answer

            sys.stdout.write(answer(harness))


def _main(event: str, harness: str | None) -> spooler.ReplaySummary | None:
    if event != "replay":
        # Claude Code runs this: nothing may reach stderr. Notes about pre-rename names (the `assay` command,
        # ASSAY_* variables) are held and written to hook.log once the event turns out to be a worker's.
        config.hold_notices()
    try:
        spool = spooler._spool_dir()
        spool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool.mkdir(exist_ok=True, mode=0o700)
        if event == "replay":
            return spooler.replay_now(spool)
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError(f"hook input exceeds {MAX_INPUT} characters")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        if harness:
            from .harnesses import report_input

            data = report_input(harness, data)
        if data.get("hook_event_name") == "SubagentStop" and data.get("agent_type") == "":
            # Claude Code runs compaction as a subagent whose SubagentStop has an empty agent_type (observed on
            # 2.1.330, scripts/qa_boundary_hooks.sh). Its summary repeats the conversation and is nobody's report,
            # so not even BENCHBOOK_HOOK_AGENTS=* or [report] agents = ["*"] captures it.
            return None
        findings_only = False
        if not is_worker(_agent_type(data)):
            # The plugin hook fires for every subagent; BENCHBOOK_HOOK_AGENTS decides which ones are captured. Leave
            # before reading config, spooling or importing anything heavier. Without it, [report] agents may take a
            # subagent's report for its findings only (T05).
            if env("HOOK_AGENTS") is not None or event not in REPORT_EVENTS:
                return None
            cfg = config.load(Path(data.get("cwd") or os.getcwd()))
            if not cfg.out_of_scope_heading or not _listed(_agent_type(data), cfg.report_agents):
                return None
            findings_only = True
        cwd = Path(data.get("cwd") or os.getcwd())
        cfg = config.load(cwd)
        project = cfg.project
        _log_notices()  # after load: BENCHBOOK_PROJECT is read there
        if project:
            # The capture decision is made here, under the environment Claude Code ran the hook with. A later
            # `benchbook hook replay` from a shell with another BENCHBOOK_HOOK_AGENTS must not undo it.
            entry = {"event": event, "project": project, "data": data, "spooled_at": time.time(), "accepted": True}
            if findings_only:
                entry["findings_only"] = True
            if cfg.out_of_scope_heading:  # read with the project, so a replay looks for the section this run had
                entry["out_of_scope_heading"] = cfg.out_of_scope_heading
            spooler._write_spool(spool, entry)
        else:
            spooler.log(f"{event}: not captured: no benchbook project for {cwd} (no .benchbook/config.toml there or "
                        f"above, and BENCHBOOK_PROJECT unset; agent_type={data.get('agent_type')!r})")
        if not spooler._locked(spool):
            subprocess.Popen([sys.executable, "-m", "benchbook.hooks", str(spool)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
    except Exception as e:  # noqa: BLE001
        spooler.log("hook spool failed: " + traceback.format_exc())
        _log_notices()
        if event == "replay":
            s = spooler.ReplaySummary()
            s.errors.append(f"replay failed: {type(e).__name__}: {e}")
            s.queued = 1
            return s
    return None


# ---------------------------------------------------------------------- which runs count


def _worker_types() -> set[str]:
    """BENCHBOOK_HOOK_AGENTS, or the plugin's worker. The worker's two names stand for each other: an install whose
    setting or plugin still says assay-worker keeps capturing when the other side is updated first."""
    raw = env("HOOK_AGENTS")
    types = {t.strip() for t in raw.split(",") if t.strip()} if raw is not None else set(WORKER_TYPES)
    return types | WORKER_TYPES if types & WORKER_TYPES else types


def _agent_type(data: dict[str, Any]) -> str | None:
    if data.get("tool_name") in ("Agent", "Task"):
        ti = data.get("tool_input")
        t = ti.get("subagent_type") if isinstance(ti, dict) else None
    else:
        t = data.get("agent_type")
    return str(t) if t else None


def is_worker(agent_type: str | None, *, require_type: bool = True) -> bool:
    """The agent type must be a worker type, with or without its plugin prefix (`benchbook:benchbook-worker`, or
    `assay:assay-worker` from a plugin installed before the rename). The hook fires for every subagent, so a new
    event without a type (older Claude Code, an Agent call without subagent_type) is not captured;
    BENCHBOOK_HOOK_AGENTS=* captures everything, untyped events included.
    require_type=False is for entries already in the spool: they passed the filter of the build that wrote them
    (earlier builds relied on the plugin matcher and spooled untyped events), so replay must not drop them."""
    types = _worker_types()
    if "*" in types:
        return True
    if not agent_type:
        return not require_type
    return agent_type in types or agent_type.rsplit(":", 1)[-1] in types


REPORT_EVENTS = ("subagent-stop", "subagent-handback")


def _listed(agent_type: str | None, agents: list[str]) -> bool:
    """[report] agents: "*" lists every subagent, typed or not; a type matches with or without its plugin prefix."""
    if "*" in agents:
        return True
    return bool(agent_type) and (agent_type in agents or str(agent_type).rsplit(":", 1)[-1] in agents)


def _is_handback(data: dict[str, Any]) -> bool:
    return data.get("tool_name") == "SubagentHandback"


def _marker(data: dict[str, Any]) -> Path | None:
    if not data.get("agent_id"):
        return None
    key = hashlib.sha256(json.dumps([data.get("session_id"), data["agent_id"]]).encode()).hexdigest()[:32]
    return spooler._state_dir() / "handback" / key


def _mark_handback(data: dict[str, Any]) -> None:
    if (m := _marker(data)) is not None:
        try:
            m.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            m.touch()
            for old in m.parent.iterdir():
                if old.stat().st_mtime < time.time() - MARKER_TTL:
                    old.unlink(missing_ok=True)
        except OSError:
            pass


def process(event: str, data: dict[str, Any], cwd: str | None = None, *, project: str | None = None,
            fired_at: float | None = None, accepted: bool = False, out_of_scope_heading: str | None = None,
            findings_only: bool = False) -> str:
    """Returns 'skipped:<reason>', 'cited' or 'captured:<claim id>'. accepted: the hook entry point already decided
    to capture this event (spool entries record it), so the current BENCHBOOK_HOOK_AGENTS is not consulted again.
    out_of_scope_heading: [report] out_of_scope_heading as the hook read it; without a project the config of the
    working directory supplies both. In a code-pack project its findings are recorded whether or not the report
    cites its own records (code/findings.py); the return value stays about the report itself."""
    if not accepted and not is_worker(_agent_type(data), require_type=False):
        return f"skipped:not-a-worker ({_agent_type(data)})"
    text = extract_text(data).replace("\x00", "").strip()  # Postgres text cannot hold NUL
    if _is_handback(data):
        _mark_handback(data)
    elif data.get("hook_event_name") == "SubagentStop" or "last_assistant_message" in data:
        if (m := _marker(data)) is not None and m.exists():
            return "skipped:handed-back"  # the handback carried the report; this is only the closing text
    if not text:
        return "skipped:no-text"
    if data.get("tool_name") in ("Agent", "Task") and isinstance(data.get("tool_response"), dict) \
            and data["tool_response"].get("status") == "async_launched":
        return "skipped:async-launch"
    if project is None:
        cfg = config.load(Path(data.get("cwd") or cwd or os.getcwd()))
        project, out_of_scope_heading = cfg.project, out_of_scope_heading or cfg.out_of_scope_heading
    if not project:
        spooler.log(f"{event}: not captured: no benchbook project for {data.get('cwd') or cwd or os.getcwd()}")
        return "skipped:no-project"
    from ..config import default_agent
    from ..core.ledger import Ledger

    lg = Ledger.open(project=project, agent=default_agent("hook"), role="harness")
    if lg.token is not None:
        spooler.remember_writer(lg.project_id, lg.agent)
    when = datetime.fromtimestamp(fired_at, UTC) if fired_at else datetime.now(UTC)
    source = {"session_id": data.get("session_id"), "event": data.get("hook_event_name") or event,
              "agent_id": data.get("agent_id"), "agent_type": data.get("agent_type"),
              "tool_name": data.get("tool_name"), "harness": data.get("harness"),
              "harness_version": data.get("harness_version")}
    if out_of_scope_heading and lg.pack.name == "code":
        _capture_findings(lg, event, text, out_of_scope_heading, data, when,
                          {k: v for k, v in source.items() if v})
    if findings_only:  # taken by [report] agents, not as a worker: no note of the report itself (T05)
        return "findings-only"
    if _cites_own_record(lg, text, data, when):
        return "cited"
    head = " ".join(text.split())[:STATEMENT_HEAD]
    # The display excerpt can match independently written reports. The digest covers the entire text.
    digest = hashlib.sha256(CAPTURE_DOMAIN + text.encode()).hexdigest()
    r = lg._propose_claim("note", "[auto-captured] " + head,
                          payload={"text": text[:PAYLOAD_TEXT], "source": {k: v for k, v in source.items() if v}},
                          tags=["auto-captured"], _dedup_hash=digest)
    return f"captured:c:{str(r.id)[:8]}"


def _capture_findings(lg: Any, event: str, text: str, heading: str, data: dict[str, Any], fired_at: datetime,
                      source: dict[str, Any]) -> None:
    """The out-of-scope section of the report as finding claims (code/findings.py). The claims are written by whoever
    the hook writes as (the token's agent under a token); source names the subagent that reported them."""
    from ..code import findings

    if not findings.parse(text, heading):
        return  # no section, or nothing in it: no transcript read, no writes
    cwd = data.get("cwd")
    r = findings.capture(lg, text, heading, source=source,
                         piece=_run_piece(lg, data, fired_at) or _reported_piece(cwd, text),
                         root=str(cwd) if cwd else None)
    spooler.log(f"{event}: out-of-scope findings: {r.findings} ({r.new} new) {' '.join(r.claims)}")


def _reported_piece(cwd: str | None, text: str) -> str | None:
    """The piece the report's own commit names (verify_requests.reported_target), for a subagent that recorded no
    attempt: the same piece the automatic re-run of this report verifies."""
    from ..checks.run import find_repo
    from ..checks.verify_requests import reported_target

    repo = find_repo(Path(cwd)) if cwd else None
    target = reported_target(repo, text) if repo is not None else None
    return target[1] if target is not None else None


def _run_piece(lg: Any, data: dict[str, Any], fired_at: datetime) -> str | None:
    """The piece this run worked on, when the ledger knows it: config.piece of the attempts this subagent recorded
    (write receipts in its own transcript), if they name exactly one piece."""
    from ..errors import BenchbookError

    _, receipts = _run_receipts(data, fired_at)
    ids = []
    for ref in receipts:
        if ref.startswith("e:"):
            try:
                ids.append(lg.resolve(ref, ["experiment"])[1])
            except BenchbookError:
                continue
    if not ids:
        return None
    rows = lg.sql_internal("SELECT DISTINCT config ->> 'piece' AS piece FROM benchbook.experiments"
                           " WHERE project_id = %s AND id = ANY(%s) AND config ->> 'piece' IS NOT NULL",
                           (lg.project_id, ids))
    return str(rows[0]["piece"]) if len(rows) == 1 else None
