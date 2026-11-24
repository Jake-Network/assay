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
from uuid import UUID, uuid4

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
SPOOL_IDENTITY_DOMAIN = b"benchbook:spool-writer\0"
WRITER_INFO = "writer.info"  # in a token's spool: its target and writer, and the agent the ledger said it is
ADOPTED_PREFIX = "adopted-"  # entries moved from the spool of the agent's earlier token
ADOPTED_DONE = "adopted.done"  # ids of adopted entries already processed (_adopt never copies them again)


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


def writer_identity() -> str | None:
    """Who an entry is written as when it reaches the ledger: under BENCHBOOK_TOKEN the token's agent, so a digest
    of the token (never the token itself); without one the owner connection, None. Two agents that share a database
    URL and a state directory but not a token must never record each other's entries: the token, not the process
    that happens to drain the spool, decides the author."""
    token = env("TOKEN")
    if not token:
        return None
    return hashlib.sha256(SPOOL_IDENTITY_DOMAIN + token.encode()).hexdigest()[:32]


def _target_parts() -> list[Any]:
    url = env("DATABASE_URL")
    data_dir = env("DATA_DIR")
    xdg_dir = os.environ.get("XDG_DATA_HOME")
    embedded = [
        str(Path(d).expanduser().resolve()) if d else None
        for d in (data_dir, xdg_dir)
    ] if not url else []
    return [url, *embedded]


def _target_key() -> str:
    """The database target alone: the spool key without a token."""
    return hashlib.sha256(json.dumps(_target_parts()).encode()).hexdigest()


def _spool_dir() -> Path:
    # Different database targets must never pick up one another's pending writes, nor different tokens on one target
    # (writer_identity). The key is built from the values, not the variable names, so entries queued under ASSAY_*
    # variables are found; without a token it is the key spools had before tokens.
    return _state_dir() / "spool" / _spool_name(writer_identity())


def _spool_name(writer: str | None) -> str:
    parts = _target_parts()
    if writer is not None:
        parts.append({"writer": writer})
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _read_info(path: Path) -> dict[str, Any] | None:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(info, dict) or not isinstance(info.get("target"), str) or not isinstance(info.get("writer"), str)
            or not isinstance(info.get("agents"), dict)):
        return None
    return info


def remember_writer(project: str, agent: str) -> None:
    """Note, in this token's spool, which agent the ledger said the token is in project (WRITER_INFO). Called once a
    ledger has accepted the token. After the token is rotated, `hook replay` under the new token adopts the entries
    queued under the old one when they are the same agent's (_adopt); the old token no longer authenticates, so
    this note, written while it did, is what says whose they are. Best effort: a failure here loses nothing queued."""
    writer = writer_identity()
    if writer is None:
        return
    try:
        spool = _spool_dir()
        path = spool / WRITER_INFO
        info = _read_info(path) or {"target": _target_key(), "writer": writer, "agents": {}}
        if info["writer"] != writer or info["agents"].get(project) == agent:
            return
        info["agents"][project] = agent
        spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_info(path, info)
    except OSError:
        pass


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
    adopted: int = 0  # entries moved here from the spool of this agent's earlier token
    held: int = 0  # entries under another token on this target that cannot be attributed to an agent
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
        return not self.dead and not self.queued and not self.held

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
                "requeued": self.requeued, "adopted": self.adopted, "held": self.held, "errors": self.errors}

    def __str__(self) -> str:
        head = (f"processed {self.processed} (captured {self.captured}, cited {self.cited}, recorded {self.recorded}, "
                f"skipped {self.skipped});"
                f" dead {self.dead}; still queued {self.queued}"
                + (f"; adopted {self.adopted} from this agent's earlier token" if self.adopted else "")
                + (f"; held {self.held} queued under a token no processor confirmed" if self.held else ""))
        return "\n".join([head, *self.errors])


def main(event: str, harness: str | None = None) -> ReplaySummary | None:
    """Spool atomically and detach without waiting for Postgres. `replay` drains in the foreground instead and
    returns a summary (the caller prints it). harness: `--harness codex|cursor` from that harness's manifest; its
    subagent-stop input is read into Claude Code's shape first (harnesses.report_input)."""
    try:
        return _main(event, harness)
    finally:
        if harness and event != "replay":
            from .harnesses import answer

            sys.stdout.write(answer(harness))


def _main(event: str, harness: str | None) -> ReplaySummary | None:
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


# A spool has two lanes, each drained by its own detached processor under its own lock: re-runs (verify-request,
# tens of seconds each) and everything else (reports, check runs, Bash hook attempts, a few milliseconds each), so
# a worker's attempt is not recorded only after every queued re-run. Before each re-run the verify lane settles the
# records lane, so whatever was queued before a request is in the ledger when it runs, as with one processor.
RECORDS, VERIFY = "records", "verify"
LANES = (RECORDS, VERIFY)
_LOCK_FILES = {RECORDS: ".lock", VERIFY: ".verify.lock"}


def start_processor(spool: Path, lane: str = RECORDS) -> None:
    """Start the detached processor for this spool's lane unless one is draining it already (check run and the Bash
    hook queue attempts in the records lane, the agent-result hook re-runs in the verify lane)."""
    if not _locked(spool, lane):
        subprocess.Popen([sys.executable, "-m", "benchbook.hooks", str(spool), lane], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


@contextmanager
def _lock(spool: Path, blocking: bool, lane: str = RECORDS) -> Iterator[bool]:
    import fcntl

    with (spool / _LOCK_FILES[lane]).open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _locked(spool: Path, lane: str = RECORDS) -> bool:
    """Whether a processor is draining this lane of the spool now. It rescans after releasing the lock, so it sees an
    entry written before this check."""
    with _lock(spool, blocking=False, lane=lane) as got:
        return not got


@contextmanager
def _all_lanes(spool: Path, blocking: bool) -> Iterator[bool]:
    """Both lanes' locks, the verify lane's first (the order the verify lane takes them in, so they never deadlock).
    Without blocking, False when either processor is running."""
    with _lock(spool, blocking, VERIFY) as got_verify, _lock(spool, blocking, RECORDS) as got_records:
        yield got_verify and got_records


def _lane(path: Path) -> str:
    """The lane an entry belongs to; one that cannot be read goes to the records lane, which quarantines it."""
    try:
        return VERIFY if json.loads(path.read_text(encoding="utf-8")).get("event") == "verify-request" else RECORDS
    except (OSError, ValueError, AttributeError):
        return RECORDS


def _write_spool(spool: Path, entry: dict[str, Any], name: str | None = None) -> None:
    """Queue one entry, bound to the identity that queued it (writer_identity): only a processor or replay under the
    same identity records it. name: a fixed file name (an adopted entry's); otherwise a new one. Under a token the
    spool also gets its WRITER_INFO (target and writer, no agent yet), so a replay under a later token can tell
    whose database this queue is for even if the ledger was never reached."""
    writer = writer_identity()
    entry = {**entry, "writer": writer, "receipt_id": entry.get("receipt_id") or uuid4().hex}
    if writer is not None and not (spool / WRITER_INFO).exists():
        try:
            _write_info(spool / WRITER_INFO, {"target": _target_key(), "writer": writer, "agents": {}}, create=True)
        except OSError as e:
            log(f"could not describe hook spool {spool}: {e}")
    with tempfile.NamedTemporaryFile(mode="w", dir=spool, encoding="utf-8", delete=False) as f:
        path = Path(f.name)
        try:
            json.dump(entry, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            path.replace(spool / name if name else path.with_suffix(".json"))
        finally:
            path.unlink(missing_ok=True)


def _write_info(path: Path, info: dict[str, Any], *, create: bool = False) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(info, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            if create:
                try:
                    os.link(tmp, path)
                except FileExistsError:
                    pass  # a processor may already have confirmed the agent
            else:
                tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


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
            fired_at: float | None = None, accepted: bool = False, out_of_scope_heading: str | None = None,
            findings_only: bool = False) -> str:
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
    if lg.token is not None:
        remember_writer(lg.project_id, lg.agent)
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
    """The out-of-scope section of the report as finding claims (findings.py). The claims are written by whoever
    the hook writes as (the token's agent under a token); source names the subagent that reported them."""
    from . import findings

    if not findings.parse(text, heading):
        return  # no section, or nothing in it: no transcript read, no writes
    cwd = data.get("cwd")
    r = findings.capture(lg, text, heading, source=source,
                         piece=_run_piece(lg, data, fired_at) or _reported_piece(cwd, text),
                         root=str(cwd) if cwd else None)
    log(f"{event}: out-of-scope findings: {r.findings} ({r.new} new) {' '.join(r.claims)}")


def _reported_piece(cwd: str | None, text: str) -> str | None:
    """The piece the report's own commit names (verify_requests.reported_target), for a subagent that recorded no
    attempt: the same piece the automatic re-run of this report verifies."""
    from .check_run import find_repo
    from .verify_requests import reported_target

    repo = find_repo(Path(cwd)) if cwd else None
    target = reported_target(repo, text) if repo is not None else None
    return target[1] if target is not None else None


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


def _entry_receipt(path: Path, entry: dict[str, Any]) -> UUID:
    if (receipt := entry.get("receipt_id")) is not None:
        if not isinstance(receipt, str):
            raise ValueError("invalid hook receipt")
        return UUID(receipt)
    if path.name.startswith(ADOPTED_PREFIX):
        return UUID(path.stem.removeprefix(ADOPTED_PREFIX))
    # Older entries have only a file name. Preserve the id earlier versions used when moving that file.
    spool = path.parent.parent if path.parent.name == "dead" else path.parent
    return UUID(hex=hashlib.sha256(f"{spool.name}/{path.name}".encode()).hexdigest()[:32])


def process_file(path: Path) -> str:
    """'ok:<process result>', 'dead:<reason>' (moved to dead/ or quarantined), 'retry:<reason>' (left queued) or
    'held:<reason>' (left queued, not retried: another identity queued it)."""
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
        writer = spooled.get("writer")  # absent: queued before entries were bound, when there were no tokens
        if writer is not None and not isinstance(writer, str):
            raise ValueError("invalid writer in hook spool entry")
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
    if writer != writer_identity():
        # Queued under another token (or with none): recording it now would name this process's agent as the
        # author of another agent's report or run. It stays queued for a replay under the identity that queued it.
        return ("held:queued under " + ("another BENCHBOOK_TOKEN" if writer else "no BENCHBOOK_TOKEN")
                + "; replay it with the environment it was queued in (after a token rotation, `benchbook hook "
                "replay` with the agent's new token adopts it)")
    adopted_id = path.stem.removeprefix(ADOPTED_PREFIX) if path.name.startswith(ADOPTED_PREFIX) else None
    if adopted_id is not None and adopted_id in _adopted_done(path.parent):
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            return f"retry:recorded, but could not remove {path.name}: {e}"
        return "ok:skipped:already recorded"
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

            result = record_spooled(event, data, spooled["project"], _entry_receipt(path, spooled))
        elif event == "verify-request":
            from .verify_requests import process as verify_request

            spool = path.parent.parent if path.parent.name == "dead" else path.parent
            result = verify_request(data, spooled["project"], _entry_receipt(path, spooled), spool)
        else:
            heading = spooled.get("out_of_scope_heading")
            result = process(event, data, spooled.get("cwd"), project=spooled.get("project"),
                             fired_at=spooled.get("spooled_at"), accepted=accepted,
                             out_of_scope_heading=heading if isinstance(heading, str) else None,
                             findings_only=spooled.get("findings_only") is True)
        log(f"{event}: {result}")
        if adopted_id is not None:  # before the removal: _adopt must never copy it again
            with (path.parent / ADOPTED_DONE).open("a", encoding="utf-8") as f:
                f.write(adopted_id + "\n")
                f.flush()
                os.fsync(f.fileno())
        path.unlink(missing_ok=True)
        if result.startswith(("captured", "recorded")) and isinstance(project := spooled.get("project"), str):
            from .code_status import refresh_cache

            refresh_cache(path.parent.parent if path.parent.name == "dead" else path.parent, project)
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


def _pending(spool: Path, lane: str | None = None) -> list[Path]:
    """The queued entries, of one lane or of both."""
    # Handbacks first: they mark their run, so its SubagentStop closing text is not captured as well.
    def key(p: Path) -> tuple[int, float]:
        try:
            first = '"tool_name": "SubagentHandback"' in p.read_text(encoding="utf-8")
            return (0 if first else 1, p.stat().st_mtime)
        except (OSError, UnicodeError):
            return (1, 0.0)

    paths = spool.glob("*.json")
    return sorted(paths if lane is None else (p for p in paths if _lane(p) == lane), key=key)


def _settle_records(spool: Path) -> None:
    """Record what waits in the records lane (one pass, under its lock) before a re-run starts."""
    with _lock(spool, blocking=True, lane=RECORDS):
        _drain(spool, (), ReplaySummary(), lane=RECORDS)


def _drain(spool: Path, delays: tuple[float, ...], summary: ReplaySummary,
           attempted: Collection[str] = (), lane: str | None = None) -> set[str]:
    seen: set[str] = set()
    left: list[tuple[Path, str]] = []
    for delay in (0, *delays):
        if delay:
            time.sleep(delay)
        left = []  # the last pass decides what is still queued
        for path in _pending(spool, lane):
            if path.name in attempted:
                continue
            seen.add(path.name)
            if lane == VERIFY:
                _settle_records(spool)
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


def replay(spool: Path, lane: str = RECORDS) -> None:
    """Background processor of one lane: drain with retries, unless another processor already holds that lane."""
    while True:
        with _lock(spool, blocking=False, lane=lane) as got:
            if not got:
                return
            seen = _drain(spool, RETRY_DELAYS, ReplaySummary(), lane=lane)
        # an entry written while the lock was held got no processor of its own
        if not any(p.name not in seen for p in _pending(spool, lane)):
            return


def replay_now(spool: Path) -> ReplaySummary:
    """`benchbook hook replay`: try each pending or dead report once in the foreground."""
    dead = spool / "dead"
    summary = ReplaySummary()
    with _all_lanes(spool, blocking=True):
        _adopt(spool, summary)
        if dead.is_dir():
            for p in dead.glob("*.json"):
                p.replace(spool / p.name)
                summary.requeued += 1
        attempted = _drain(spool, (), summary)
    # A hook that enqueued after the snapshot may have seen our lock and skipped starting a processor.
    while any(p.name not in attempted for p in spool.glob("*.json")):
        with _all_lanes(spool, blocking=True):
            attempted.update(_drain(spool, (), summary, attempted))
    log(f"replay {spool}: {summary}")  # doctor reads which spool a clean replay settled
    return summary


def _adopted_done(spool: Path) -> set[str]:
    try:
        return set((spool / ADOPTED_DONE).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def _copy_exists(spool: Path, name: str) -> bool:
    """The adopted copy is still waiting here, in dead/, or quarantined."""
    stem = name[:-len(".json")]
    return any(p.exists() for p in (spool / name, spool / "dead" / name, spool / f"{stem}.invalid"))


def _adopt(spool: Path, summary: ReplaySummary) -> None:
    """Move here the entries queued under this agent's earlier token (rotated or replaced), so they are recorded
    under its current one. An entry is the agent's when the old spool's WRITER_INFO, written while the old token
    authenticated, names for the entry's project the same agent the ledger says the current token is. Entries of
    other agents and of other database targets stay where they are; entries whose token no processor ever
    confirmed stay too, and are counted as held, with the reason.

    The copy keeps its original receipt as its file name (adopted-<id>.json) across rotations. If the copy already
    exists or is marked as done, only the original is removed. The old spool's lock is held meanwhile, so its own
    processor does not move the same files."""
    me = writer_identity()
    if me is None or not spool.parent.is_dir():
        return
    from .errors import InvalidAgentToken

    target = _target_key()
    current: dict[str, str | None] = {}
    unavailable: dict[str, str] = {}
    done = _adopted_done(spool)

    def agent_in(project: str) -> str | None:
        if project not in current:
            try:
                from .ledger import Ledger

                lg = Ledger.open(project=project)  # the current token's agent, or an error
                current[project] = lg.agent
                remember_writer(project, lg.agent)
            except InvalidAgentToken:
                # A token belongs to one project. Other projects' queues on this DB are not ours to adopt.
                current[project] = None
            except Exception as e:  # noqa: BLE001 - not confirmed, so nothing is moved
                unavailable[project] = type(e).__name__
                current[project] = None
        return current[project]

    for old in sorted(p for p in spool.parent.iterdir() if p.is_dir() and p != spool):
        if old.name == target:  # the owner spool has no token
            continue
        info = _read_info(old / WRITER_INFO)
        if info is not None and (info["target"] != target or info["writer"] == me):
            continue
        with _all_lanes(old, blocking=False) as got:
            if not got:
                summary.errors.append(f"{old.name}: its processor is running; entries queued there under an earlier "
                                      "token were not looked at (run replay again)")
                continue
            info = _read_info(old / WRITER_INFO)
            if info is not None and (info["target"] != target or info["writer"] == me):
                continue
            dead = old / "dead"
            unconfirmed = 0
            old_done = _adopted_done(old)
            for path in [*old.glob("*.json"), *(dead.glob("*.json") if dead.is_dir() else [])]:
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    project = entry["project"]
                    if not isinstance(project, str):
                        continue
                    receipt = _entry_receipt(path, entry)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if info is None:
                    old_writer = entry.get("writer")
                    if isinstance(old_writer, str) and old_writer != me and old.name == _spool_name(old_writer):
                        unconfirmed += 1
                    continue
                if entry.get("writer") != info["writer"]:
                    continue  # not queued under the token that spool belongs to
                known = info["agents"].get(project)
                if not isinstance(known, str):
                    unconfirmed += 1
                    continue
                agent = agent_in(project)
                if project in unavailable:
                    summary.queued += 1
                if agent != known:
                    continue  # another agent's, or identity could not be checked
                mid = receipt.hex
                name = f"{ADOPTED_PREFIX}{mid}.json"
                if mid not in done and mid not in old_done and not _copy_exists(spool, name):
                    entry["receipt_id"] = mid
                    _write_spool(spool, entry, name=name)  # bound to the current token
                    summary.adopted += 1
                    log(f"replay: adopted {path.name} from {old.name} as {name} ({known} in {project})")
                try:
                    path.unlink(missing_ok=True)
                except OSError as e:
                    summary.queued += 1
                    summary.errors.append(f"{old.name}/{path.name}: adopted, but the original could not be removed "
                                          f"({e}); the next replay removes it and copies nothing")
        if unconfirmed:
            summary.held += unconfirmed
            summary.errors.append(
                f"{old.name}: {unconfirmed} entr{'y' if unconfirmed == 1 else 'ies'} queued under another token on "
                "this database, which no processor ever confirmed as an agent (the ledger was never reached with "
                f"it); held there, not recorded. If it was this agent's token before a rotation, these are its "
                f"reports: record them by hand from {old}")
    for project, reason in unavailable.items():
        summary.errors.append(f"could not confirm the current token's agent in {project} ({reason}); "
                              "earlier entries are still queued")


if __name__ == "__main__":
    path = Path(sys.argv[1])
    if path.is_dir():
        replay(path, sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in LANES else RECORDS)
    else:
        print(process_file(path))
