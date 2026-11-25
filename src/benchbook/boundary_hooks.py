"""Boundary hooks (T09): what the ledger says, carried across a session start, a compaction and a turn.

- `benchbook hook session-start` (SessionStart, every source): the brief scoped by what this branch changed
  (`changed_scope`, the code pack's `brief --here`), at most 1,500 tokens and 6,000 characters, as additionalContext.
  The same text goes to `.benchbook/STATE.md`, the time, HEAD, project and ledger on its first line, for harnesses
  without hooks and for people. When the ledger cannot be reached (no answer within 5 seconds, a refused
  connection), the last STATE.md can be given instead. This requires the same project and database target and a
  session without a Benchbook token. For a remote database it also requires an identifiable connection target and
  a transport error, as a deadline alone could hide a login refusal. A brief whose sections are all empty is
  written to STATE.md but not given.
- `benchbook hook pre-compact` (PreCompact): the same brief cut to three lines per section, as one paragraph of at
  most 1,000 characters on stdout, which Claude Code reads as instructions for the compaction summary. Nothing when
  the ledger does not answer.
- `benchbook hook notes` (UserPromptSubmit, and PostToolUse of the tools whose own hooks do not deliver them): notes
  queued for this session (`<state dir>/notes/<session_id>/`, written by the automatic re-run, T11), given once and
  moved to `delivered/`. Only the lead gets them: a hook that fired inside a subagent (an `agent_id` in its input)
  leaves them for the lead's next hook.

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
    data: dict[str, Any] = {}
    ran: str | None = None
    try:
        data = read_input()
        if harness != "claude-code":
            from .harnesses import boundary_input

            data = boundary_input(harness, data)
        lead = session_notes.for_lead(data)
        # Read before any note moves: an answer that cannot be built after the move would lose the notes.
        ran = data.get("hook_event_name") if isinstance(data.get("hook_event_name"), str) else None
        if event == "notes":
            text = session_notes.deliver(data.get("session_id")) if lead else ""
        elif event == "session-start":
            text = session_start(data)
        elif event == "pre-compact":
            text = pre_compact(data)
        elif event == "agent-result":
            response = data.get("tool_response")
            if not (isinstance(response, dict) and response.get("status") == "async_launched"):
                from .verify_requests import enqueue  # not for a launch: it has no report, and the import costs

                text = enqueue(data)
            if data.get("hook_event_name") == "SubagentStop":
                return  # SubagentStop has no context for the lead; pending notes wait for a hook that has
        if event in ("session-start", "agent-result") and lead:
            # An async_launched result delivers too: a lead fanning work out calls Agent more than anything else.
            notes = session_notes.deliver(data.get("session_id"), min(MAX_CHARS, 10_000 - len(text) - 2))
            text = "\n\n".join(part for part in (text, notes) if part)
    except Exception:  # noqa: BLE001 - a hook must not fail the session
        import traceback

        _log(f"{event} failed: " + traceback.format_exc())
        text = ""
    if not text and harness == "claude-code":
        return  # Claude Code's empty answer is no output; harnesses.py is not imported for it
    from .harnesses import context_answer

    sys.stdout.write(context_answer(harness, event, text, ran))
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


def ledger_id() -> str | None:
    """Fingerprint the connection target and login without storing credentials. Unknown targets cannot be reused."""
    import hashlib

    if url := config.env("DATABASE_URL"):
        from psycopg import ProgrammingError
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.pq import Conninfo

        try:
            supplied = conninfo_to_dict(url)
        except ProgrammingError:
            return None
        defaults = {opt.keyword.decode(): os.fsdecode(opt.val) for opt in Conninfo.get_defaults() if opt.val}
        if supplied.get("service") or defaults.get("service"):
            return None  # libpq can get the endpoint and login from a service file
        fields = ("host", "hostaddr", "port", "user", "dbname")
        host, addr, port, user, database = [str(supplied.get(name) or defaults.get(name, "")) for name in fields]
        if not (host or addr) or not port or not user or any("," in value for value in (host, addr, port)):
            return None  # implicit socket directory or a list of candidate endpoints
        key = ["postgresql", host, addr, port, user, database or user]
    else:
        from .db.conn import data_dir

        key = ["embedded", str(data_dir().resolve())]
    return hashlib.sha256(json.dumps(key).encode("utf-8")).hexdigest()


_STATE_HEAD = re.compile(r"benchbook state · written [^·]+ · HEAD \S+ · project (\S+) · ledger ([0-9a-f]{64}) · ")


def write_state(w: Where, brief: str) -> None:
    """STATE.md: the brief under a first line with the time it was written, the HEAD it was written at, the project
    and the ledger (ledger_id). Kept out of git status (.git/info/exclude) when the benchbook folder itself is
    tracked."""
    from datetime import UTC, datetime

    head = _git(w.repo, "rev-parse", "--short=12", "HEAD") or "none"
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%MZ")
    w.local.mkdir(parents=True, exist_ok=True)
    tmp = w.state.with_name(f".{STATE_FILE}.{os.getpid()}.tmp")
    target = ledger_id() if not config.env("TOKEN") else None
    tmp.write_text(f"benchbook state · written {stamp} · HEAD {head} · project {w.project} · "
                   f"ledger {target or 'unavailable for fallback'} · rewritten at each session start\n\n{brief}",
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
    """The last STATE.md under one line saying the ledger did not answer; "" when there is none, or when it was
    written for another project or from another ledger (or by a version that did not say which)."""
    from datetime import UTC, datetime

    if config.env("TOKEN"):
        return ""
    try:
        text = w.state.read_text(encoding="utf-8")
        written = datetime.fromtimestamp(w.state.stat().st_mtime, UTC).strftime("%Y-%m-%d %H:%MZ")
    except OSError:
        return ""
    head = _STATE_HEAD.match(text)
    try:
        same = head is not None and head[1] == w.project and head[2] == ledger_id()
    except Exception as e:  # noqa: BLE001 - a ledger that cannot be named is not this one
        _log(f"STATE.md not used: the ledger could not be named: {type(e).__name__}: {e}")
        return ""
    if not same:
        _log(f"STATE.md not used: it was written for another project or ledger than {w.project}'s")
        return ""
    return clip(f"benchbook: the ledger did not answer ({why}); this is STATE.md as of {written}.\n\n{text}",
                MAX_CHARS)


def unreachable(e: BaseException) -> bool:
    """Require a transport error that proves the server did not answer."""
    import psycopg

    if not isinstance(e, psycopg.OperationalError) or e.sqlstate is not None:
        return False
    text = str(e).lower()
    if any(word in text for word in ("fatal:", "authentication", "password", "pg_hba.conf", "permission denied")):
        return False
    return any(detail in text for detail in ("connection refused", "network is unreachable", "no route to host",
                                              "could not translate host name")) or (
        "on socket " in text and "no such file or directory" in text
    )


class LedgerFailed(Exception):
    """compile_brief failed; down: the ledger could not be reached (unreachable), so the last STATE.md may stand in."""

    def __init__(self, down: bool) -> None:
        super().__init__("unreachable" if down else "refused")
        self.down = down


def _brief_or_none(w: Where, event: str) -> str:
    """The brief (written to STATE.md as well). LedgerFailed when the ledger did not answer in time or failed."""
    try:
        brief = within(lambda: compile_brief(w), TIMEOUT)
    except TimeoutError as e:
        _log(f"{event}: {e}; ledger did not answer")
        raise LedgerFailed(not config.env("DATABASE_URL")) from e
    except Exception as e:  # noqa: BLE001 - reported, and given no brief
        down = unreachable(e)
        _log(f"{event}: the ledger {'could not be reached' if down else 'failed'}: {type(e).__name__}: "
             f"{str(e)[:300]}")
        raise LedgerFailed(down) from e
    try:
        write_state(w, brief)
    except Exception as e:  # noqa: BLE001 - the brief is still given
        _log(f"{event}: STATE.md not written: {type(e).__name__}: {e}")
    return brief


def lead_guide(data: dict[str, Any], w: Where) -> str:
    """What a lead needs to know to use benchbook in a swarm, at a session's start (not a resume: it is still in the
    context) in a code project ([checks] declared). Given even when the ledger is empty: a first session's lead
    learns it nowhere else (the boundary pilot's arm F lead had no skill, and its workers reported no findings and it
    recorded no dead ends)."""
    if data.get("source") == "resume" or w.repo is None:
        return ""
    try:
        cfg = config.load(w.repo)
    except Exception:  # noqa: BLE001 - an unreadable config.toml gives no guide
        return ""
    if not cfg.checks:
        return ""
    heading = cfg.out_of_scope_heading
    found = (f" and to list what it noticed outside its piece under a `## {heading}` heading (recorded as findings)"
             if heading else "")
    return "\n".join([
        "benchbook records this repository's checks and reports. If you hand pieces to subagents:",
        f"- Ask each to end its report with `DONE <full commit sha>`{found}.",
        "- Merge a piece after its verdict line (`benchbook: <piece> <sha>: re-run passed ...`); a `git merge` of a "
        "reported commit waits for it.",
        "- Write down what you learn yourself: `benchbook --agent lead discard <piece> --approach NAME --why \"...\"` "
        "for a dead end, `benchbook --agent lead finding add \"...\" --file PATH` for a problem outside the pieces.",
    ])


def session_start(data: dict[str, Any]) -> str:
    w = where(data)
    if w is None:
        return ""
    guide = lead_guide(data, w)
    try:
        brief = _brief_or_none(w, "session-start")
    except LedgerFailed as e:
        if not e.down:
            return guide
        cause = e.__cause__
        why = (f"no answer within {TIMEOUT:g} s" if isinstance(cause, TimeoutError)
               else f"unreachable: {type(cause).__name__}")
        return from_state(w, why)
    if is_empty(brief):
        return guide
    return f"{guide}\n\n{clip(brief, MAX_CHARS - len(guide) - 2)}" if guide else brief


def pre_compact(data: dict[str, Any]) -> str:
    w = where(data)
    if w is None:
        return ""
    try:
        brief = _brief_or_none(w, "pre-compact")
    except LedgerFailed:
        return ""
    if is_empty(brief):
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
