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

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

MAX_INPUT = 2_000_000
STATEMENT_HEAD = 300
PAYLOAD_TEXT = 4000


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


def main(event: str) -> None:
    """Hook entry point: spool and detach. Never raises, never blocks."""
    try:
        raw = sys.stdin.read(MAX_INPUT)
        spool = _state_dir() / "spool"
        spool.mkdir(parents=True, exist_ok=True)
        path = spool / f"{time.time_ns()}-{os.getpid()}.json"
        path.write_text(json.dumps({"event": event, "cwd": os.getcwd(), "input": raw}), encoding="utf-8")
        if os.environ.get("ASSAY_HOOK_SYNC") == "1":
            process_file(path)
            return
        subprocess.Popen([sys.executable, "-m", "assay.hooks", str(path)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         close_fds=True)
    except Exception:  # noqa: BLE001
        log("hook spool failed: " + traceback.format_exc())


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


def process(event: str, data: dict[str, Any], cwd: str | None = None) -> str:
    """Returns 'skipped:<reason>', 'cited' or 'captured:<claim id>'."""
    from .config import load
    from .ids import SHORT_ID_RE

    text = extract_text(data).strip()
    if not text:
        return "skipped:no-text"
    if data.get("tool_name") in ("Agent", "Task") and isinstance(data.get("tool_response"), dict) \
            and data["tool_response"].get("status") == "async_launched":
        return "skipped:async-launch"
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
    r = lg.propose_claim("note", "[auto-captured] " + head,
                         payload={"text": text[:PAYLOAD_TEXT], "source": {k: v for k, v in source.items() if v}},
                         tags=["auto-captured"])
    return f"captured:c:{str(r.id)[:8]}"


def process_file(path: Path) -> None:
    try:
        spooled = json.loads(path.read_text(encoding="utf-8"))
        data = json.loads(spooled["input"] or "{}")
        result = process(spooled["event"], data if isinstance(data, dict) else {}, spooled.get("cwd"))
        log(f"{spooled['event']}: {result}")
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        log(f"hook processing failed for {path} (kept for inspection): " + traceback.format_exc())


if __name__ == "__main__":
    process_file(Path(sys.argv[1]))
