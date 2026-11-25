"""What a subagent's report and its transcript say: the report's text, and whether the run cited a record it wrote
itself (an experiment or claim id that a benchbook write of this run returned). A run that did is not captured again."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import config

MAX_TRANSCRIPT = 20_000_000
# Transcript timestamps come from the client clock, created_at from the database clock.
CLOCK_SKEW = timedelta(minutes=2)
CITABLE = {"e": "experiments", "c": "claims"}
# Transcripts written before the rename (and sessions still running the old plugin) name the tools, the command and
# the agent variable with the old prefix; a worker that cited its own record there must still count as having cited.
WRITE_TOOLS = {"benchbook_record_experiment": "e", "benchbook_propose_claim": "c",
               "assay_record_experiment": "e", "assay_propose_claim": "c"}
CLI_WRITES = {("exp", "record"): "e", ("exp", "run"): "e", ("claim", "add"): "c"}
COMMANDS = (config.NAME, config.LEGACY_NAME)
AGENT_ASSIGNMENTS = (config.ENV_PREFIX + "AGENT=", config.LEGACY_ENV_PREFIX + "AGENT=")


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
    from ..core.ids import SHORT_ID_RE

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
    from ..core.ids import SHORT_ID_RE
    from ..errors import BenchbookError

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
