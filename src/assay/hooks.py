"""Claude Code hook adapter (§12.3): make sure a subagent's result reaches the ledger.

`assay hook subagent-stop` reads the hook input (JSON on stdin), spools it to a file and starts a detached
processor (unless one is already draining that spool), so the hook itself returns within milliseconds and always
exits 0. The processor extracts the subagent's final text; unless it cites an experiment or claim id this run
recorded, it records an auto-captured note. Task ids (t:) never count as a citation: naming the task you worked
on is not recording its result.

Inputs handled:
- SubagentStop: last_assistant_message
- PostToolUse on Agent/Task: tool_response (text blocks)
- PostToolUse on SubagentHandback: tool_input.message (the report, when handed back through that tool)

Only assay workers are captured: the agent type must be one of ASSAY_HOOK_AGENTS (default `assay-worker`, matched
with or without a plugin prefix such as `assay:`). A run that handed its report back is captured once, from the
handback. Permanent failures (unknown project, schema newer than this assay, data Postgres refuses) move to
`dead/` in the spool; `assay hook replay` retries them once more and reports what it did.
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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MAX_INPUT = 2_000_000
STATEMENT_HEAD = 300
PAYLOAD_TEXT = 4000
RETRY_DELAYS = (1, 2, 4, 8, 15, 30)
MAX_TRANSCRIPT = 20_000_000
# Without a transcript to date the run, ids recorded this long before the hook fired still count as this run's.
FALLBACK_WINDOW = timedelta(hours=6)
# Transcript timestamps come from the client clock, created_at from the database clock.
CLOCK_SKEW = timedelta(minutes=2)
MARKER_TTL = 86400
CITABLE = {"e": "experiments", "c": "claims"}


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "assay"


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
    url = os.environ.get("ASSAY_DATABASE_URL")
    data_dir = os.environ.get("ASSAY_DATA_DIR")
    xdg_dir = os.environ.get("XDG_DATA_HOME")
    embedded = [
        str(Path(d).expanduser().resolve()) if d else None
        for d in (data_dir, xdg_dir)
    ] if not url else []
    key = hashlib.sha256(json.dumps([url, *embedded]).encode()).hexdigest()
    return _state_dir() / "spool" / key


@dataclass
class ReplaySummary:
    """What one drain of the spool did. `errors` has one line per entry left queued or moved to dead/."""
    captured: int = 0
    cited: int = 0
    skipped: int = 0
    dead: int = 0
    queued: int = 0
    requeued: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.captured + self.cited + self.skipped

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
            else:
                self.skipped += 1
        elif kind == "dead":
            self.dead += 1
            self.errors.append(f"{path.name}: {detail} (moved to dead/)")
        else:
            self.queued += 1
            self.errors.append(f"{path.name}: {detail} (still queued)")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "processed": self.processed, "captured": self.captured, "cited": self.cited,
                "skipped": self.skipped, "dead": self.dead, "queued": self.queued, "requeued": self.requeued,
                "errors": self.errors}

    def __str__(self) -> str:
        head = (f"processed {self.processed} (captured {self.captured}, cited {self.cited}, skipped {self.skipped});"
                f" dead {self.dead}; still queued {self.queued}")
        return "\n".join([head, *self.errors])


def main(event: str) -> ReplaySummary | None:
    """Spool atomically and detach without waiting for Postgres. `replay` drains in the foreground instead and
    returns a summary (the caller prints it)."""
    try:
        spool = _spool_dir()
        spool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool.mkdir(exist_ok=True, mode=0o700)
        if event == "replay":
            return replay_now(spool)
        from .config import load

        raw = sys.stdin.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError(f"hook input exceeds {MAX_INPUT} characters")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        cwd = Path(data.get("cwd") or os.getcwd())
        project = load(cwd).project
        if project:
            _write_spool(spool, {"event": event, "project": project, "data": data, "spooled_at": time.time()})
        else:
            log(f"{event}: not captured: no assay project for {cwd} (no .assay/config.toml there or above, and "
                f"ASSAY_PROJECT unset; agent_type={data.get('agent_type')!r})")
        if not _locked(spool):
            subprocess.Popen([sys.executable, "-m", "assay.hooks", str(spool)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True)
    except Exception as e:  # noqa: BLE001
        log("hook spool failed: " + traceback.format_exc())
        if event == "replay":
            s = ReplaySummary()
            s.errors.append(f"replay failed: {type(e).__name__}: {e}")
            s.queued = 1
            return s
    return None


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
        ti = data.get("tool_input") or {}
        return str(ti.get("message") or "")
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
    return {t.strip() for t in os.environ.get("ASSAY_HOOK_AGENTS", "assay-worker").split(",") if t.strip()}


def _agent_type(data: dict[str, Any]) -> str | None:
    if data.get("tool_name") in ("Agent", "Task"):
        t = (data.get("tool_input") or {}).get("subagent_type")
    else:
        t = data.get("agent_type")
    return str(t) if t else None


def is_worker(agent_type: str | None) -> bool:
    """No agent type (the main session, older Claude Code) is let through; a named one must be a worker type,
    with or without its plugin prefix (`assay:assay-worker`)."""
    if not agent_type:
        return True
    types = _worker_types()
    return "*" in types or agent_type in types or agent_type.rsplit(":", 1)[-1] in types


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


_AGENT_ARG = re.compile(r"(?:ASSAY_AGENT=|--agent[= ])['\"]?([A-Za-z0-9_.:@-]+)")


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


def run_window(data: dict[str, Any]) -> tuple[datetime | None, set[str]]:
    """When this subagent run started, and the assay agent names it wrote as. Read from its own transcript
    (SubagentStop: agent_transcript_path; otherwise the nested subagents/ file) or, for a PostToolUse on Agent,
    from the parent transcript line that launched it. (None, {}) when nothing is readable."""
    start: datetime | None = None
    authors: set[str] = set()
    candidates: list[Path] = []
    if data.get("agent_transcript_path"):
        candidates.append(Path(str(data["agent_transcript_path"])))
    if data.get("transcript_path") and data.get("agent_id"):
        tp = Path(str(data["transcript_path"]))
        candidates.append(tp.with_suffix("") / "subagents" / f"agent-{data['agent_id']}.jsonl")
    for path in candidates:
        for row in _transcript_lines(path):
            t = _ts(row.get("timestamp"))
            if t is not None and (start is None or t < start):
                start = t
            for b in _blocks(row):
                if b.get("type") != "tool_use" or not isinstance(b.get("input"), dict):
                    continue
                inp = b["input"]
                if "assay" in str(b.get("name", "")) and isinstance(inp.get("agent"), str):
                    authors.add(inp["agent"].strip())
                if isinstance(inp.get("command"), str) and "assay" in inp["command"]:
                    authors.update(_AGENT_ARG.findall(inp["command"]))
        if start is not None:
            return start, authors
    tool_use_id = data.get("tool_use_id")
    if data.get("tool_name") in ("Agent", "Task") and tool_use_id and data.get("transcript_path"):
        for row in _transcript_lines(Path(str(data["transcript_path"]))):
            if any(b.get("type") == "tool_use" and b.get("id") == tool_use_id for b in _blocks(row)):
                return _ts(row.get("timestamp")), authors
    return start, authors


def _cites_own_record(lg: Any, text: str, data: dict[str, Any], fired_at: datetime) -> bool:
    from .errors import AssayError
    from .ids import SHORT_ID_RE

    start, authors = run_window(data)
    since = (start or fired_at - FALLBACK_WINDOW) - CLOCK_SKEW
    for pfx, body in SHORT_ID_RE.findall(text):
        if pfx not in CITABLE:
            continue
        try:
            kind, rid = lg.resolve(f"{pfx}:{body}")
        except AssayError:
            continue
        rows = lg.sql_internal(f"SELECT author, created_at FROM assay.{CITABLE[pfx]} WHERE id = %s", (rid,))
        if not rows:
            continue
        author, created = rows[0]["author"], rows[0]["created_at"]
        if author == "hook" or created < since:
            continue
        if authors and author not in authors:
            continue
        return True
    return False


def process(event: str, data: dict[str, Any], cwd: str | None = None, *, project: str | None = None,
            fired_at: float | None = None) -> str:
    """Returns 'skipped:<reason>', 'cited' or 'captured:<claim id>'."""
    from .config import load

    if not is_worker(_agent_type(data)):
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
        project = load(Path(data.get("cwd") or cwd or os.getcwd())).project
    if not project:
        log(f"{event}: not captured: no assay project for {data.get('cwd') or cwd or os.getcwd()}")
        return "skipped:no-project"
    from .ledger import Ledger

    lg = Ledger.open(project=project, agent="hook", role="harness")
    when = datetime.fromtimestamp(fired_at, UTC) if fired_at else datetime.now(UTC)
    if _cites_own_record(lg, text, data, when):
        return "cited"
    head = " ".join(text.split())[:STATEMENT_HEAD]
    source = {"session_id": data.get("session_id"), "event": data.get("hook_event_name") or event,
              "agent_id": data.get("agent_id"), "agent_type": data.get("agent_type"),
              "tool_name": data.get("tool_name")}
    # The display excerpt can match independently written reports. The digest covers the entire text.
    digest = hashlib.sha256(b"assay:auto-captured\0" + text.encode()).hexdigest()
    r = lg._propose_claim("note", "[auto-captured] " + head,
                          payload={"text": text[:PAYLOAD_TEXT], "source": {k: v for k, v in source.items() if v}},
                          tags=["auto-captured"], _dedup_hash=digest)
    return f"captured:c:{str(r.id)[:8]}"


# ---------------------------------------------------------------------- the spool


def _permanent(e: BaseException) -> bool:
    """Errors a retry cannot fix: a ledger rule (unknown project, schema newer than this assay), or data and
    SQL errors. Connection trouble and OS errors are transient."""
    import psycopg

    from .errors import AssayError

    if isinstance(e, AssayError):
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
    except OSError as e:
        log(f"cannot read hook spool {path} (queued to retry): " + traceback.format_exc())
        return f"retry:{type(e).__name__}: {e}"
    except (KeyError, TypeError, ValueError) as e:
        path.rename(path.with_suffix(".invalid"))
        log(f"invalid hook spool {path} (quarantined): " + traceback.format_exc())
        return f"dead:invalid spool entry ({e})"
    try:
        result = process(event, data, spooled.get("cwd"), project=spooled.get("project"),
                         fired_at=spooled.get("spooled_at"))
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
        except OSError:
            return (1, 0.0)

    return sorted(spool.glob("*.json"), key=key)


def _drain(spool: Path, delays: tuple[float, ...]) -> tuple[ReplaySummary, set[str]]:
    summary = ReplaySummary()
    seen: set[str] = set()
    left: list[tuple[Path, str]] = []
    for delay in (0, *delays):
        if delay:
            time.sleep(delay)
        left = []  # the last pass decides what is still queued
        for path in _pending(spool):
            seen.add(path.name)
            outcome = process_file(path)
            if outcome.startswith("retry:"):
                left.append((path, outcome))
            else:
                summary.add(path, outcome)
        if not left:
            break
    for path, outcome in left:
        summary.add(path, outcome)
    return summary, seen


def replay(spool: Path) -> None:
    """Background processor: drain with retries, unless another processor already holds the spool."""
    while True:
        with _lock(spool, blocking=False) as got:
            if not got:
                return
            _, seen = _drain(spool, RETRY_DELAYS)
        # an entry written while the lock was held got no processor of its own
        if not any(p.name not in seen for p in spool.glob("*.json")):
            return


def replay_now(spool: Path) -> ReplaySummary:
    """`assay hook replay`: give dead entries one more try, drain once in the foreground, report."""
    dead = spool / "dead"
    with _lock(spool, blocking=True):
        requeued = 0
        if dead.is_dir():
            for p in dead.glob("*.json"):
                p.replace(spool / p.name)
                requeued += 1
        summary, _ = _drain(spool, ())
    summary.requeued = requeued
    log(f"replay: {summary}")
    return summary


if __name__ == "__main__":
    path = Path(sys.argv[1])
    if path.is_dir():
        replay(path)
    else:
        print(process_file(path))
