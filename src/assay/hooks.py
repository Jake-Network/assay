"""Claude Code hook adapter (§12.3): make sure a subagent's result reaches the ledger.

`assay hook subagent-stop` reads the hook input (JSON on stdin), spools it to a file and starts a detached
processor, so the hook itself returns within milliseconds and always exits 0. The processor extracts the
subagent's final text; if it cites no existing assay record id (e:/c:/t:), it records an auto-captured note.

Inputs handled:
- SubagentStop: last_assistant_message
- PostToolUse on Agent/Task: tool_response (text blocks)
- PostToolUse on SubagentHandback: tool_input.message (the report, when handed back through that tool)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

MAX_INPUT = 2_000_000
STATEMENT_HEAD = 300
PAYLOAD_TEXT = 4000
RETRY_DELAYS = (1, 2, 4, 8, 15, 30)


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


def main(event: str) -> None:
    """Spool atomically and detach without waiting for Postgres."""
    try:
        spool = _spool_dir()
        spool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        spool.mkdir(exist_ok=True, mode=0o700)
        if event != "replay":
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
                _write_spool(spool, {"event": event, "project": project, "data": data})
        subprocess.Popen([sys.executable, "-m", "assay.hooks", str(spool)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)
    except Exception:  # noqa: BLE001
        log("hook spool failed: " + traceback.format_exc())


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


def process(event: str, data: dict[str, Any], cwd: str | None = None, *, project: str | None = None) -> str:
    """Returns 'skipped:<reason>', 'cited' or 'captured:<claim id>'."""
    from .config import load
    from .ids import SHORT_ID_RE

    text = extract_text(data).strip()
    if not text:
        return "skipped:no-text"
    if data.get("tool_name") in ("Agent", "Task") and isinstance(data.get("tool_response"), dict) \
            and data["tool_response"].get("status") == "async_launched":
        return "skipped:async-launch"
    if project is None:
        project = load(Path(data.get("cwd") or cwd or os.getcwd())).project
    if not project:
        return "skipped:no-project"
    from .errors import AssayError
    from .ledger import Ledger

    lg = Ledger.open(project=project, agent="hook", role="harness")
    for pfx, body in SHORT_ID_RE.findall(text):
        if pfx not in "ect":
            continue
        try:
            lg.resolve(f"{pfx}:{body}")
            return "cited"
        except AssayError:
            continue
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


def process_file(path: Path) -> bool:
    """Return whether the entry was handled or quarantined. Leave transient failures queued."""
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
    except OSError:
        log(f"cannot read hook spool {path} (queued to retry): " + traceback.format_exc())
        return False
    except (KeyError, TypeError, ValueError):
        path.rename(path.with_suffix(".invalid"))
        log(f"invalid hook spool {path} (quarantined): " + traceback.format_exc())
        return True
    try:
        result = process(event, data, spooled.get("cwd"), project=spooled.get("project"))
        log(f"{spooled['event']}: {result}")
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log(f"hook processing failed for {path} (queued to retry): " + traceback.format_exc())
        return False
    return True


def replay(spool: Path) -> None:
    """Drain this database's pending captures, including results retained after earlier failures."""
    import fcntl

    with (spool / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for delay in (0, *RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            handled = [process_file(path) for path in sorted(spool.glob("*.json"))]
            if all(handled):
                break


if __name__ == "__main__":
    path = Path(sys.argv[1])
    if path.is_dir():
        replay(path)
    else:
        process_file(path)
