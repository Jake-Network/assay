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
import re
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config
from .config import env

MAX_INPUT = 2_000_000
STATEMENT_HEAD = 300
PAYLOAD_TEXT = 4000
RETRY_DELAYS = (1, 2, 4, 8, 15, 30)
MAX_TRANSCRIPT = 20_000_000
# Transcript timestamps come from the client clock, created_at from the database clock.
CLOCK_SKEW = timedelta(minutes=2)
MARKER_TTL = 86400
CITABLE = {"e": "experiments", "c": "claims"}
# Transcripts written before the rename (and sessions still running the old plugin) name the tools, the command and
# the agent variable with the old prefix; a worker that cited its own record there must still count as having cited.
WRITE_TOOLS = {"benchbook_record_experiment": "e", "benchbook_propose_claim": "c",
               "assay_record_experiment": "e", "assay_propose_claim": "c"}
CLI_WRITES = {("exp", "record"): "e", ("exp", "run"): "e", ("claim", "add"): "c"}
COMMANDS = (config.NAME, config.LEGACY_NAME)
AGENT_ASSIGNMENTS = (config.ENV_PREFIX + "AGENT=", config.LEGACY_ENV_PREFIX + "AGENT=")
# the worker subagent of the plugin, under its current name and the one it had before the rename
WORKER_TYPES = frozenset({"benchbook-worker", "assay-worker"})
# Domain separator of the auto-capture dedup hash. It is stored with every captured note, so it keeps the old name:
# a report captured before the upgrade and replayed after it must hash to the same claim.
CAPTURE_DOMAIN = b"assay:auto-captured\0"
# Spool entries that are check attempts (benchbook check run, the Bash hook); attempts.record_spooled records them.
ATTEMPT_EVENTS = frozenset({"check-run", "bash-result"})


def _state_dir() -> Path:
    return config.state_dir()


def log(msg: str) -> None:
    try:
        d = _state_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "hook.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _spool_dir() -> Path:
    # Different database targets must never pick up one another's pending writes.
    # The key is built from the values, not the variable names, so entries queued under ASSAY_* variables are found.
    url = env("DATABASE_URL")
    data_dir = env("DATA_DIR")
    xdg_dir = os.environ.get("XDG_DATA_HOME")
    embedded = [
        str(Path(d).expanduser().resolve()) if d else None
        for d in (data_dir, xdg_dir)
    ] if not url else []
    key = hashlib.sha256(json.dumps([url, *embedded]).encode()).hexdigest()
    return _state_dir() / "spool" / key


def _log_notices() -> None:
    """Write the notes held back from stderr (config.hold_notices) to hook.log."""
    for note in config.take_notices():
        log(f"note: {note}")


@dataclass
class ReplaySummary:
    """What one drain of the spool did. `errors` has one line per entry left queued or moved to dead/."""
    captured: int = 0
    cited: int = 0
    recorded: int = 0  # check attempts
    skipped: int = 0
    dead: int = 0
    queued: int = 0
    requeued: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.captured + self.cited + self.recorded + self.skipped

    @property
    def ok(self) -> bool:
        return not self.dead and not self.queued

    def add(self, path: Path, outcome: str) -> None:
        kind, _, detail = outcome.partition(":")
        if kind == "ok":
            res = detail.split(":", 1)[0]
            if res == "captured":
                self.captured += 1
            elif res == "cited":
                self.cited += 1
            elif res == "recorded":
                self.recorded += 1
            else:
                self.skipped += 1
        elif kind == "dead":
            self.dead += 1
            location = "quarantined as .invalid" if detail.startswith("invalid spool entry") else "moved to dead/"
            self.errors.append(f"{path.name}: {detail} ({location})")
        else:
            self.queued += 1
            self.errors.append(f"{path.name}: {detail} (still queued)")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "processed": self.processed, "captured": self.captured, "cited": self.cited,
                "recorded": self.recorded, "skipped": self.skipped, "dead": self.dead, "queued": self.queued,
                "requeued": self.requeued, "errors": self.errors}

    def __str__(self) -> str:
        head = (f"processed {self.processed} (captured {self.captured}, cited {self.cited}, recorded {self.recorded}, "
                f"skipped {self.skipped});"
                f" dead {self.dead}; still queued {self.queued}")
        return "\n".join([head, *self.errors])


def main(event: str) -> ReplaySummary | None:
    """Spool atomically and detach without waiting for Postgres. `replay` drains in the foreground instead and
    returns a summary (the caller prints it)."""
    if event != "replay":
        # Claude Code runs this: nothing may reach stderr. Notes about pre-rename names (the `assay` command,
        # ASSAY_* variables) are held and written to hook.log once the event turns out to be a worker's.
        config.hold_notices()
    try:
        spool = _spool_dir()
        spool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool.mkdir(exist_ok=True, mode=0o700)
        if event == "replay":
            return replay_now(spool)
        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError(f"hook input exceeds {MAX_INPUT} characters")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        if not is_worker(_agent_type(data)):
            # The plugin hook fires for every subagent; BENCHBOOK_HOOK_AGENTS decides which ones are captured. Leave
            # before reading config, spooling or importing anything heavier.
            return None
        cwd = Path(data.get("cwd") or os.getcwd())
        cfg = config.load(cwd)
        project = cfg.project
        _log_notices()  # after load: BENCHBOOK_PROJECT is read there
        if project:
            # The capture decision is made here, under the environment Claude Code ran the hook with. A later
            # `benchbook hook replay` from a shell with another BENCHBOOK_HOOK_AGENTS must not undo it.
            entry = {"event": event, "project": project, "data": data, "spooled_at": time.time(), "accepted": True}
            if cfg.out_of_scope_heading:  # read with the project, so a replay looks for the section this run had
                entry["out_of_scope_heading"] = cfg.out_of_scope_heading
            _write_spool(spool, entry)
        else:
            log(f"{event}: not captured: no benchbook project for {cwd} (no .benchbook/config.toml there or above, and "
                f"BENCHBOOK_PROJECT unset; agent_type={data.get('agent_type')!r})")
        if not _locked(spool):
            subprocess.Popen([sys.executable, "-m", "benchbook.hooks", str(spool)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
    except Exception as e:  # noqa: BLE001
        log("hook spool failed: " + traceback.format_exc())
        _log_notices()
        if event == "replay":
            s = ReplaySummary()
            s.errors.append(f"replay failed: {type(e).__name__}: {e}")
            s.queued = 1
            return s
    return None


def start_processor(spool: Path) -> None:
    """Start the detached processor for this spool unless one is draining it already (check run and the Bash hook
    queue attempts here too)."""
    if not _locked(spool):
        subprocess.Popen([sys.executable, "-m", "benchbook.hooks", str(spool)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


@contextmanager
def _lock(spool: Path, blocking: bool) -> Iterator[bool]:
    import fcntl

    with (spool / ".lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked(spool: Path) -> bool:
    """Whether a processor is draining this spool now. It rescans after releasing the lock, so it sees an entry
    written before this check."""
    with _lock(spool, blocking=False) as got:
        return not got


def _write_spool(spool: Path, entry: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=spool, encoding="utf-8", delete=False) as f:
        path = Path(f.name)
        try:
            json.dump(entry, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            path.replace(path.with_suffix(".json"))
        finally:
            path.unlink(missing_ok=True)


def extract_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("last_assistant_message"), str):
        return str(data["last_assistant_message"])
    tool = data.get("tool_name")
    if tool == "SubagentHandback":
        ti = data.get("tool_input")
        return str(ti.get("message") or "") if isinstance(ti, dict) else ""
    if tool in ("Agent", "Task"):
        return _flatten(data.get("tool_response"))
    return ""


def _flatten(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(t for t in (_flatten(x) for x in v) if t)
    if isinstance(v, dict):
        if v.get("type") == "text" and isinstance(v.get("text"), str):
            return str(v["text"])
        for k in ("content", "result", "text", "message"):
            if k in v:
                return _flatten(v[k])
    return ""


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


def _is_handback(data: dict[str, Any]) -> bool:
    return data.get("tool_name") == "SubagentHandback"


def _marker(data: dict[str, Any]) -> Path | None:
    if not data.get("agent_id"):
        return None
    key = hashlib.sha256(json.dumps([data.get("session_id"), data["agent_id"]]).encode()).hexdigest()[:32]
    return _state_dir() / "handback" / key


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


# ---------------------------------------------------------------------- what this run recorded


def _ts(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        t = datetime.fromisoformat(v)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _transcript_lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        if not path.is_file() or path.stat().st_size > MAX_TRANSCRIPT:
            return
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def _blocks(row: dict[str, Any]) -> list[dict[str, Any]]:
    msg = row.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _write_tool(block: dict[str, Any]) -> tuple[str, str | None, bool] | None:
    """Identify calls that return the ID of a newly created experiment or claim."""
    import shlex

    inp = block.get("input")
    name = str(block.get("name", ""))
    if not isinstance(inp, dict):
        return None
    tool = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    if tool in WRITE_TOOLS:
        author = inp.get("agent")
        if isinstance(author, str) and author.strip():
            return WRITE_TOOLS[tool], author.strip(), True
    shell = inp.get("command")
    if name != "Bash" or not isinstance(shell, str) or "\n" in shell:
        return None
    try:
        lexer = shlex.shlex(shell, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        command = list(lexer)
    except ValueError:
        return None
    # A series of shell commands can return unrelated output after the Benchbook call.
    if any(all(c in "();<>|&" for c in part) for part in command if part):
        return None
    args = iter(command)
    cmd = next(args, "")
    author = None
    while "=" in cmd and cmd.split("=", 1)[0].isidentifier():
        if cmd.startswith(AGENT_ASSIGNMENTS):
            author = cmd.split("=", 1)[1]
        cmd = next(args, "")
    if Path(cmd).name == "uv" and next(args, "") == "run":
        cmd = next(args, "")
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(cmd).name) and next(args, "") == "-m":
        if next(args, "") in tuple(m for c in COMMANDS for m in (c, c + ".cli")):
            cmd = config.NAME
    if Path(cmd).name not in COMMANDS:
        return None
    positional: list[str] = []
    for arg in args:
        if arg in ("--agent", "--project"):
            value = next(args, "")
            if arg == "--agent":
                author = value
        elif arg.startswith("--agent="):
            author = arg.split("=", 1)[1]
        elif arg != "--json" and not arg.startswith("--project=") and len(positional) < 2:
            positional.append(arg)
    prefix = CLI_WRITES.get((positional[0], positional[1])) if len(positional) == 2 else None
    return (prefix, author, False) if prefix else None


def _receipt(text: str, prefix: str, is_mcp: bool) -> str | None:
    """Accept a new ID from a write response. A duplicate response refers to an already existing record: its id
    is that record (claims) or missing (a refused experiment). A repeat recorded in notify mode is new: its id
    differs from duplicate_of."""
    from .ids import SHORT_ID_RE

    try:
        result = json.loads(text)
    except ValueError:
        result = None if is_mcp else _exp_run_receipt(text)
    if isinstance(result, dict):
        ref = result.get("id")
        dup = result.get("duplicate_of")
        if result.get("ok") is False or (dup is not None and (ref is None or ref == dup)):
            return None
    elif not is_mcp and prefix == "e":
        match = re.fullmatch(r"(e:[0-9a-f]{8}) (?:improved|no_change|worse|failed|unscored)(?:  ★ new best)?", text)
        ref = match[1] if match else None
    else:
        ref = text if not is_mcp else None
    if isinstance(ref, str) and ref.startswith(prefix + ":") and SHORT_ID_RE.fullmatch(ref):
        return ref
    return None


def _exp_run_receipt(text: str) -> dict[str, Any] | None:
    """`benchbook exp run` prints its receipt after the command's own output (and Claude Code may append stderr):
    the last line that starts with "benchbook: ", or with --json the last JSON line that has "ran"."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if m := re.match(r"benchbook: recorded (e:[0-9a-f]{8}) ", line):
            return {"id": m[1]}
        if line.startswith("benchbook: "):
            return {"ok": False}  # not run, or not recorded
        if line.startswith("{"):
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if isinstance(result, dict) and "ran" in result:
                return result
    return None


def _run_receipts(data: dict[str, Any], fired_at: datetime) -> tuple[datetime | None, dict[str, str | None]]:
    """Read the run start and successful writing tool receipts from this subagent's own transcript."""
    candidates: list[Path] = []
    if data.get("agent_transcript_path"):
        candidates.append(Path(str(data["agent_transcript_path"])))
    if data.get("transcript_path") and data.get("agent_id"):
        tp = Path(str(data["transcript_path"]))
        candidates.append(tp.with_suffix("") / "subagents" / f"agent-{data['agent_id']}.jsonl")
    for path in candidates:
        start: datetime | None = None
        calls: dict[str, tuple[str, str | None, bool]] = {}
        receipts: dict[str, str | None] = {}
        for row in _transcript_lines(path):
            t = _ts(row.get("timestamp"))
            if t is None or t > fired_at + CLOCK_SKEW:
                continue
            if start is None or t < start:
                start = t
            for b in _blocks(row):
                if b.get("type") == "tool_use" and isinstance(b.get("id"), str):
                    if write := _write_tool(b):
                        calls[b["id"]] = write
                elif b.get("type") == "tool_result":
                    tool_id = b.get("tool_use_id")
                    if isinstance(tool_id, str) and (call := calls.pop(tool_id, None)) and not b.get("is_error"):
                        prefix, author, is_mcp = call
                        if ref := _receipt(_flatten(b.get("content")).strip(), prefix, is_mcp):
                            receipts[ref] = author
        if receipts:
            return start, receipts
    return None, {}


def _cites_own_record(lg: Any, text: str, data: dict[str, Any], fired_at: datetime) -> bool:
    from .errors import BenchbookError
    from .ids import SHORT_ID_RE

    cited = [(pfx, f"{pfx}:{body}") for pfx, body in SHORT_ID_RE.findall(text) if pfx in CITABLE]
    if not cited:
        return False
    start, receipts = _run_receipts(data, fired_at)
    if start is None:
        return False
    for pfx, ref in cited:
        if ref not in receipts:
            continue
        try:
            _, rid = lg.resolve(ref)
        except BenchbookError:
            continue
        rows = lg.sql_internal(f"SELECT author, created_at FROM benchbook.{CITABLE[pfx]} WHERE id = %s", (rid,))
        if not rows:
            continue
        author, created = rows[0]["author"], rows[0]["created_at"]
        expected_author = receipts[ref]
        if (author != "hook" and (not expected_author or author == expected_author)
                and start - CLOCK_SKEW <= created <= fired_at + CLOCK_SKEW):
            return True
    return False


def process(event: str, data: dict[str, Any], cwd: str | None = None, *, project: str | None = None,
            fired_at: float | None = None, accepted: bool = False, out_of_scope_heading: str | None = None) -> str:
    """Returns 'skipped:<reason>', 'cited' or 'captured:<claim id>'. accepted: the hook entry point already decided
    to capture this event (spool entries record it), so the current BENCHBOOK_HOOK_AGENTS is not consulted again.
    out_of_scope_heading: [report] out_of_scope_heading as the hook read it; without a project the config of the
    working directory supplies both. In a code-pack project its findings are recorded whether or not the report
    cites its own records (findings.py); the return value stays about the report itself."""
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
        log(f"{event}: not captured: no benchbook project for {data.get('cwd') or cwd or os.getcwd()}")
        return "skipped:no-project"
    from .config import default_agent
    from .ledger import Ledger

    lg = Ledger.open(project=project, agent=default_agent("hook"), role="harness")
    when = datetime.fromtimestamp(fired_at, UTC) if fired_at else datetime.now(UTC)
    source = {"session_id": data.get("session_id"), "event": data.get("hook_event_name") or event,
              "agent_id": data.get("agent_id"), "agent_type": data.get("agent_type"),
              "tool_name": data.get("tool_name")}
    if out_of_scope_heading and lg.pack.name == "code":
        _capture_findings(lg, event, text, out_of_scope_heading, data, when,
                          {k: v for k, v in source.items() if v})
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
    """The out-of-scope section of the report as finding claims (findings.py). The claims are written by whoever
    the hook writes as (the token's agent under a token); source names the subagent that reported them."""
    from . import findings

    if not findings.parse(text, heading):
        return  # no section, or nothing in it: no transcript read, no writes
    cwd = data.get("cwd")
    r = findings.capture(lg, text, heading, source=source, piece=_run_piece(lg, data, fired_at),
                         root=str(cwd) if cwd else None)
    log(f"{event}: out-of-scope findings: {r.findings} ({r.new} new) {' '.join(r.claims)}")


def _run_piece(lg: Any, data: dict[str, Any], fired_at: datetime) -> str | None:
    """The piece this run worked on, when the ledger knows it: config.piece of the attempts this subagent recorded
    (write receipts in its own transcript), if they name exactly one piece."""
    from .errors import BenchbookError

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


# ---------------------------------------------------------------------- the spool


def _permanent(e: BaseException) -> bool:
    """Errors a retry cannot fix: a ledger rule (unknown project, schema newer than this benchbook), or data and
    SQL errors. Connection trouble and OS errors are transient."""
    import psycopg

    from .errors import BenchbookError

    if isinstance(e, BenchbookError):
        return True
    if isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError, OSError, TimeoutError)):
        return False
    return isinstance(e, (psycopg.Error, ValueError, TypeError, KeyError))


def _bury(path: Path, reason: str) -> None:
    dead = path.parent / "dead"
    dead.mkdir(exist_ok=True, mode=0o700)
    path.replace(dead / path.name)
    log(f"hook entry {path.name} moved to {dead}: {reason}")


def process_file(path: Path) -> str:
    """'ok:<process result>', 'dead:<reason>' (moved to dead/ or quarantined) or 'retry:<reason>' (left queued)."""
    try:
        spooled = json.loads(path.read_text(encoding="utf-8"))
        if "data" in spooled:
            data = spooled["data"]
        else:
            # Older spools kept raw stdin and inferred the project from the working directory.
            data = json.loads(spooled["input"] or "{}")
        event = spooled["event"]
        if not isinstance(data, dict) or not isinstance(event, str):
            raise ValueError("invalid hook spool entry")
        accepted = spooled.get("accepted") is True
        agent_type = _agent_type(data)
    except OSError as e:
        log(f"cannot read hook spool {path} (queued to retry): " + traceback.format_exc())
        return f"retry:{type(e).__name__}: {e}"
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        try:
            path.rename(path.with_suffix(".invalid"))
        except OSError:
            return f"retry:could not quarantine invalid spool entry ({e})"
        log(f"invalid hook spool {path} (quarantined): " + traceback.format_exc())
        return f"dead:invalid spool entry ({e})"
    if not accepted and not is_worker(agent_type, require_type=False):
        # Queued by a build that did not record its decision: the BENCHBOOK_HOOK_AGENTS that accepted it may differ
        # from the current one. Keep it rather than drop it, so a replay with the original setting captures it.
        reason = (f"agent type {agent_type!r} is not in the current BENCHBOOK_HOOK_AGENTS and the entry predates "
                  "the recorded capture decision; to capture it run: BENCHBOOK_HOOK_AGENTS=<the setting it was queued "
                  "under> benchbook hook replay")
        try:
            _bury(path, reason)
        except OSError:
            return f"retry:{reason}"
        return f"dead:{reason}"
    try:
        if event in ATTEMPT_EVENTS:  # a check run (check run, the Bash hook): an attempt, not a report
            from .attempts import record_spooled

            result = record_spooled(event, data, spooled["project"])
        else:
            heading = spooled.get("out_of_scope_heading")
            result = process(event, data, spooled.get("cwd"), project=spooled.get("project"),
                             fired_at=spooled.get("spooled_at"), accepted=accepted,
                             out_of_scope_heading=heading if isinstance(heading, str) else None)
        log(f"{event}: {result}")
        path.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        if _permanent(e):
            log(f"hook processing failed permanently for {path}: " + traceback.format_exc())
            try:
                _bury(path, reason)
            except OSError:
                return f"retry:{reason}"
            return f"dead:{reason}"
        log(f"hook processing failed for {path} (queued to retry): " + traceback.format_exc())
        return f"retry:{reason}"
    return f"ok:{result}"


def _pending(spool: Path) -> list[Path]:
    # Handbacks first: they mark their run, so its SubagentStop closing text is not captured as well.
    def key(p: Path) -> tuple[int, float]:
        try:
            first = '"tool_name": "SubagentHandback"' in p.read_text(encoding="utf-8")
            return (0 if first else 1, p.stat().st_mtime)
        except (OSError, UnicodeError):
            return (1, 0.0)

    return sorted(spool.glob("*.json"), key=key)


def _drain(spool: Path, delays: tuple[float, ...], summary: ReplaySummary,
           attempted: Collection[str] = ()) -> set[str]:
    seen: set[str] = set()
    left: list[tuple[Path, str]] = []
    for delay in (0, *delays):
        if delay:
            time.sleep(delay)
        left = []  # the last pass decides what is still queued
        for path in _pending(spool):
            if path.name in attempted:
                continue
            seen.add(path.name)
            try:
                outcome = process_file(path)
            except Exception as e:  # noqa: BLE001 (one entry must not stop the others)
                log(f"hook entry {path} could not be processed (queued to retry): " + traceback.format_exc())
                outcome = f"retry:{type(e).__name__}: {e}"
            if outcome.startswith("retry:"):
                left.append((path, outcome))
            else:
                summary.add(path, outcome)
        if not left:
            break
    for path, outcome in left:
        summary.add(path, outcome)
    return seen


def replay(spool: Path) -> None:
    """Background processor: drain with retries, unless another processor already holds the spool."""
    while True:
        with _lock(spool, blocking=False) as got:
            if not got:
                return
            seen = _drain(spool, RETRY_DELAYS, ReplaySummary())
        # an entry written while the lock was held got no processor of its own
        if not any(p.name not in seen for p in spool.glob("*.json")):
            return


def replay_now(spool: Path) -> ReplaySummary:
    """`benchbook hook replay`: try each pending or dead report once in the foreground."""
    dead = spool / "dead"
    summary = ReplaySummary()
    with _lock(spool, blocking=True):
        if dead.is_dir():
            for p in dead.glob("*.json"):
                p.replace(spool / p.name)
                summary.requeued += 1
        attempted = _drain(spool, (), summary)
    # A hook that enqueued after the snapshot may have seen our lock and skipped starting a processor.
    while any(p.name not in attempted for p in spool.glob("*.json")):
        with _lock(spool, blocking=True):
            attempted.update(_drain(spool, (), summary, attempted))
    log(f"replay {spool}: {summary}")  # doctor reads which spool a clean replay settled
    return summary


if __name__ == "__main__":
    path = Path(sys.argv[1])
    if path.is_dir():
        replay(path)
    else:
        print(process_file(path))
