"""Boundary hooks (T09): what the ledger says, carried across a session start, a compaction and a turn.

- `benchbook hook session-start` (SessionStart, every source): the brief scoped by what this branch changed
  (`changed_scope`, the code pack's `brief --here`), at most 1,500 tokens and 6,000 characters, as additionalContext.
  The same text goes to `.benchbook/STATE.md`, the time and HEAD on its first line, for harnesses without hooks and
  for people. When the ledger does not answer within 5 seconds, the last STATE.md is given instead, under a line
  saying so; with no STATE.md, nothing. A brief whose sections are all empty is written to STATE.md but not given.
- `benchbook hook pre-compact` (PreCompact): the same brief cut to three lines per section, as one paragraph of at
  most 1,000 characters on stdout, which Claude Code reads as instructions for the compaction summary. Nothing when
  the ledger does not answer.
- `benchbook hook notes` (UserPromptSubmit): notes queued for this session (`<state dir>/notes/<session_id>/`,
  written by the automatic re-run, T11), given once and moved to `delivered/`.

A directory without a benchbook project leaves after reading config.toml, before the database layer is imported.
Nothing reaches stderr and every path exits 0; failures go to hook.log.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from . import config, session_notes

BRIEF_TOKENS = 1500
MAX_CHARS = 6000  # Claude Code replaces a longer additionalContext with a file path and a 2,000-character preview
PRECOMPACT_CHARS = 1000
PER_SECTION = 3  # lines per section in the pre-compact paragraph
TIMEOUT = 5.0  # seconds for the ledger to answer: connection, queries and the brief's git calls
STATE_FILE = "STATE.md"
MAX_INPUT = 2_000_000
GIT_TIMEOUT = 2.0
NONE_LINE = "- none recorded"
PRECOMPACT_HEAD = "Keep these ids and statuses verbatim in the summary: "
_COUNT = re.compile(r"\s*\([^()]*\)\s*$")
T = TypeVar("T")

# Set when the ledger call was left running past TIMEOUT: the entry point then ends the process without waiting for it.
abandoned = False


def _log(msg: str) -> None:
    from .hooks import log

    log(msg)


def read_input() -> dict[str, Any]:
    raw = sys.stdin.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        return {}
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def main(event: str, harness: str | None = None) -> None:
    """Run one boundary hook: read the input, write the answer to stdout. Never raises, never writes to stderr."""
    config.hold_notices()  # the harness runs this: nothing may reach stderr
    harness = harness or "claude-code"
    text = ""
    try:
        data = read_input()
        if harness != "claude-code":
            from .harnesses import boundary_input

            data = boundary_input(harness, data)
        if event == "agent-result":
            response = data.get("tool_response")
            if isinstance(response, dict) and response.get("status") == "async_launched":
                return  # The launch has no report; leave pending notes for the next hook.
        if event == "notes":
            text = session_notes.deliver(data.get("session_id"))
        elif event == "session-start":
            text = session_start(data)
        elif event == "pre-compact":
            text = pre_compact(data)
        elif event == "agent-result":
            from .verify_requests import enqueue

            text = enqueue(data)
        if event in ("session-start", "agent-result"):
            notes = session_notes.deliver(data.get("session_id"), min(MAX_CHARS, 10_000 - len(text) - 2))
            text = "\n\n".join(part for part in (text, notes) if part)
    except Exception:  # noqa: BLE001 - a hook must not fail the session
        import traceback

        _log(f"{event} failed: " + traceback.format_exc())
        text = ""
    if not text and harness == "claude-code":
        return  # Claude Code's empty answer is no output; harnesses.py is not imported for it
    from .harnesses import context_answer

    sys.stdout.write(context_answer(harness, event, text))
    sys.stdout.flush()


# ---------------------------------------------------------------------------------------------- the brief


def within(fn: Callable[[], T], timeout: float) -> T:
    """fn's value, or TimeoutError when it has not returned within timeout seconds (it is left running in a daemon
    thread, and `abandoned` tells the entry point not to wait for it)."""
    import threading

    global abandoned
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            box["error"] = e

    t = threading.Thread(target=run, daemon=True, name="benchbook-boundary")
    t.start()
    t.join(timeout)
    if t.is_alive():
        abandoned = True
        raise TimeoutError(f"no answer within {timeout:g} s")
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[no-any-return]



class Where:
    """Where a hook ran: the project, the checkout root (None outside git) and the folder STATE.md goes in."""

    def __init__(self, project: str, repo: Path | None, local: Path) -> None:
        self.project, self.repo, self.local = project, repo, local

    @property
    def state(self) -> Path:
        return self.local / STATE_FILE


def where(data: dict[str, Any]) -> Where | None:
    """The project of the hook's cwd (config.toml there or above, or BENCHBOOK_PROJECT); None without one."""
    cwd = Path(data.get("cwd") or os.getcwd())
    cfg = config.load(cwd)
    if not cfg.project:
        return None
    from .check_run import find_repo

    repo = find_repo(cwd)
    root = repo or (cfg.path.parent.parent if cfg.path is not None else cwd)
    return Where(cfg.project, repo, config.local_dir(root))


def compile_brief(w: Where) -> str:
    """The boundary brief: the code pack's scoped by changed_scope, with validity marks against this checkout."""
    from .ledger import Ledger

    lg = Ledger.open(project=w.project, agent=config.default_agent("hook"), role="harness")
    tags: list[str] = []
    warning = ""
    if lg.pack.name == "code":
        from .code_status import recording_warning
        from .packs.code.brief import changed_scope

        warning = recording_warning(w.repo or w.local.parent)
        if w.repo is not None:
            tags = changed_scope(w.repo)
    limit = MAX_CHARS - len(warning) - 2 if warning else MAX_CHARS
    text = clip(lg.brief(budget_tokens=BRIEF_TOKENS, tags=tags or None, repo=w.repo, max_chars=limit), limit)
    return f"{warning}\n\n{text}" if warning else text


def clip(text: str, limit: int) -> str:
    """At most limit characters, cut at a line end when there is one (a pack's brief without max_chars)."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 2)
    return text[: limit - 1] + "…" if cut <= 0 else text[:cut] + "\n…"


def is_empty(brief: str) -> bool:
    """True when no section lists anything: every item line is "- none recorded"."""
    return not brief.startswith("benchbook: recording stopped: ") and not any(
        line.startswith("- ") and line != NONE_LINE for line in brief.splitlines())


def _git(repo: Path | None, *args: str) -> str:
    import subprocess

    if repo is None:
        return ""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                              stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def write_state(w: Where, brief: str) -> None:
    """STATE.md: the brief under a first line with the time it was written and the HEAD it was written at. Kept
    out of git status (.git/info/exclude) when the benchbook folder itself is tracked."""
    from datetime import UTC, datetime

    head = _git(w.repo, "rev-parse", "--short=12", "HEAD") or "none"
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%MZ")
    w.local.mkdir(parents=True, exist_ok=True)
    tmp = w.state.with_name(f".{STATE_FILE}.{os.getpid()}.tmp")
    tmp.write_text(f"benchbook state · written {stamp} · HEAD {head} · rewritten at each session start\n\n{brief}",
                   encoding="utf-8")
    os.replace(tmp, w.state)
    if w.repo is not None:
        _exclude(w.repo, w.state)


def _exclude(repo: Path, path: Path) -> None:
    import subprocess

    try:
        rel = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return
    if subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", rel], capture_output=True,
                      timeout=GIT_TIMEOUT).returncode == 0:
        return
    where_ = _git(repo, "rev-parse", "--git-path", "info/exclude")
    if not where_:
        return
    exclude = Path(where_) if Path(where_).is_absolute() else repo / where_
    text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"/{rel}\n", encoding="utf-8")


def from_state(w: Where, why: str) -> str:
    """The last STATE.md under one line saying the ledger did not answer; "" when there is none."""
    from datetime import UTC, datetime

    try:
        text = w.state.read_text(encoding="utf-8")
        written = datetime.fromtimestamp(w.state.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%MZ")
    except OSError:
        return ""
    return clip(f"benchbook: the ledger did not answer ({why}); this is STATE.md as of {written}.\n\n{text}",
                MAX_CHARS)


def _brief_or_none(w: Where, event: str) -> str | None:
    """The brief (written to STATE.md as well), or None when the ledger did not answer in time or failed."""
    try:
        brief = within(lambda: compile_brief(w), TIMEOUT)
    except TimeoutError as e:
        _log(f"{event}: {e}; ledger did not answer")
        return None
    except Exception as e:  # noqa: BLE001 - an unreachable or broken ledger is the same as a silent one here
        _log(f"{event}: the ledger failed: {type(e).__name__}: {str(e)[:300]}")
        return None
    try:
        write_state(w, brief)
    except Exception as e:  # noqa: BLE001 - the brief is still given
        _log(f"{event}: STATE.md not written: {type(e).__name__}: {e}")
    return brief


def session_start(data: dict[str, Any]) -> str:
    w = where(data)
    if w is None:
        return ""
    brief = _brief_or_none(w, "session-start")
    if brief is None:
        return from_state(w, f"no answer within {TIMEOUT:g} s")
    return "" if is_empty(brief) else brief


def pre_compact(data: dict[str, Any]) -> str:
    w = where(data)
    if w is None:
        return ""
    brief = _brief_or_none(w, "pre-compact")
    if brief is None or is_empty(brief):
        return ""
    from .code_status import RECORDING_STOPPED

    if brief.startswith(RECORDING_STOPPED):
        warning = brief.splitlines()[0]
        summary = compact_paragraph(brief, PRECOMPACT_CHARS - len(warning) - 1)
        return " ".join(part for part in (warning, summary) if part)
    return compact_paragraph(brief)


def compact_paragraph(brief: str, limit: int = PRECOMPACT_CHARS) -> str:
    """The brief as one paragraph of at most limit characters: at most PER_SECTION items per section, sections in
    the brief's order, whole items only (the first item is cut when even it does not fit)."""
    sections: list[tuple[str, list[str]]] = []
    for block in brief.split("\n## ")[1:]:
        title, *lines = block.splitlines()
        items = [x[2:].strip() for x in lines if x.startswith("- ") and x != NONE_LINE][:PER_SECTION]
        if items:
            sections.append((_COUNT.sub("", title), items))
    out = PRECOMPACT_HEAD
    for title, items in sections:
        for i, item in enumerate(items):
            piece = (f" {title}: " if i == 0 else "; ") + item
            if out == PRECOMPACT_HEAD:
                piece = piece.lstrip()
            if len(out) + len(piece) + 1 > limit:
                if out == PRECOMPACT_HEAD:
                    return (out + piece)[: limit - 1] + "…"
                return out + "."
            out += piece
        out += "."
    return out if out != PRECOMPACT_HEAD else ""
