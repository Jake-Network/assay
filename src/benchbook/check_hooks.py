"""Claude Code hooks for declared checks (spec §7, layers 2 and 3).

`benchbook hook bash-result` (PostToolUse and PostToolUseFailure on Bash; `--harness codex|cursor|gemini` reads those
harnesses' shell-result events instead, harnesses.py): when a Bash command is one of the checks
declared in .benchbook/config.toml, queue the run for the ledger as an attempt. It reads config.toml and nothing
else, imports no database module and writes only on a match, so every other Bash call costs a TOML read (the
budget is 50 ms, tests/test_hooks.py). The attempt carries a rough signature: the exit code from the error's
"Exit code N" first line and a hash of the normalised last line. The output itself is never kept.

`benchbook hook task-completed` (TaskCompleted): when the piece's HEAD has no passing run of every declared check,
exit 2 so Claude Code keeps the task open, with the command to run on stderr. It fails open (exit 0 and a line in
hook.log) whenever it cannot tell: benchbook broken or the database unreachable, no project or no declared checks,
no piece, or no commit.

Matching rule (docs/claude-code.md): the command and the declaration are split into words the way the shell would
(quotes removed), leading VAR=value assignments (and `env` with them) and redirections are dropped, and the words
must then be equal. One leading `cd DIR &&` is allowed (the check runs in DIR). A word with {junit} in the declaration
matches a word of that shape or nothing. A successful raw Bash call of a JUnit check is observed only: a shared
report path cannot identify its producing invocation. Run it with `benchbook check run NAME`, which gives each
invocation its own report path and validates its report. The check must have run from the repository root.
A command that contains the check inside a pipeline or a list (`check | tail`, `a && check`) is observed but not
recorded: its exit code is not the check's. `benchbook check run NAME` is observed and not recorded either: the wrapper
records it itself.

Every match is appended to observed.jsonl in the spool directory: the denominator of the record coverage metric.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from .harnesses import ShellCall

EVENT = "bash-result"
VIA = "bash-hook"
MAX_INPUT = 2_000_000
OBSERVED = "observed.jsonl"
CONTROL = frozenset({"|", "||", "&&", ";", "&", "|&", ";;", "(", ")"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_EXIT = re.compile(r"Exit code (-?\d+)\s*$")
GLOBAL_FLAGS = ("--project", "--agent")
COMMANDS = (config.NAME, config.LEGACY_NAME)
BASE_BRANCHES = ("main", "master")


# ---------------------------------------------------------------------------------------------- matching


@dataclass
class Match:
    check: str
    kind: str  # direct (recorded here) | wrapped (benchbook check run: recorded by the wrapper) | unrecordable
    cwd: Path  # where the check ran: the hook's cwd, or the directory of a leading cd
    project: str | None = None
    reason: str = ""
    junit: bool = False


def words(command: str) -> list[str] | None:
    """Shell words with quotes removed; operators are words of their own. None when the shell syntax is beyond
    this (unbalanced quotes, several lines)."""
    if "\n" in command.strip():
        return None
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _is_op(w: str) -> bool:
    return bool(w) and all(c in "();<>|&" for c in w)


def segments(ws: list[str]) -> tuple[list[list[str]], list[str]]:
    """Simple commands and the control operators between them. Redirections and leading assignments are dropped
    from each: they change where the output goes or the environment, not which check ran."""
    segs: list[list[str]] = [[]]
    ops: list[str] = []
    i = 0
    while i < len(ws):
        w = ws[i]
        if w in CONTROL or (_is_op(w) and not any(c in w for c in "<>")):
            ops.append(w)
            segs.append([])
        elif _is_op(w):  # a redirection: drop it, its target and a file descriptor number written before it
            cur = segs[-1]
            if cur and cur[-1].isdigit():
                cur.pop()
            i += 1
        else:
            segs[-1].append(w)
        i += 1
    return [_strip_env(s) for s in segs], ops


def _strip_env(seg: list[str]) -> list[str]:
    i = 0
    if seg[:1] == ["env"]:
        i = 1
    while i < len(seg) and _ASSIGNMENT.match(seg[i]):
        i += 1
    if i == 1 and seg[:1] == ["env"]:
        return seg  # `env` alone prints the environment
    return seg[i:]


def _word_pattern(seg: list[str]) -> re.Pattern[str]:
    """A declared simple command as a pattern over NUL-separated words. A word with {junit} is optional and
    matches one word of its shape; a bare {junit} takes the option word before it along."""
    parts: list[str] = []
    for w in seg:
        if "{junit}" in w:
            pre, _, post = w.partition("{junit}")
            one = re.escape("\0" + pre) + r"[^\0]+" + re.escape(post)
            if w == "{junit}" and parts and not parts[-1].startswith("(?:"):
                option = parts.pop()  # the option word goes with the path: both or neither
                parts.append(f"(?:{option}{one})?")
            else:
                parts.append(f"(?:{one})?")
        else:
            parts.append(re.escape("\0" + w))
    return re.compile("".join(parts))


def _joined(seg: list[str]) -> str:
    return "".join("\0" + w for w in seg)


@dataclass
class Declared:
    name: str
    segs: list[re.Pattern[str]]
    ops: list[str]


def declare(checks: dict[str, Any]) -> list[Declared]:
    out = []
    for name, spec in checks.items():
        ws = words(spec.cmd)
        if not ws:
            continue
        segs, ops = segments(ws)
        if all(segs):
            out.append(Declared(name, [_word_pattern(s) for s in segs], ops))
    return out


def _equal(d: Declared, segs: list[list[str]], ops: list[str]) -> bool:
    return (ops == d.ops and len(segs) == len(d.segs)
            and all(p.fullmatch(_joined(s)) for p, s in zip(d.segs, segs, strict=True)))


def _contains(d: Declared, segs: list[list[str]], ops: list[str]) -> bool:
    k = len(d.segs)
    return any(_equal(d, segs[i:i + k], ops[i:i + k - 1]) for i in range(len(segs) - k + 1))


def wrapped_check(seg: list[str]) -> str | None:
    """The check NAME of `benchbook [global options] check run NAME ...` (also through `uv run`, `python -m benchbook`
    and the pre-rename `assay`), else None."""
    it = list(seg)
    if it[:2] == ["uv", "run"]:
        it = it[2:]
    if len(it) >= 3 and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(it[0]).name) and it[1] == "-m" \
            and it[2] in COMMANDS:
        it = [config.NAME, *it[3:]]
    if not it or Path(it[0]).name not in COMMANDS:
        return None
    i = 1
    while i < len(it):
        if it[i] == "--json" or it[i].startswith(tuple(f + "=" for f in GLOBAL_FLAGS)):
            i += 1
        elif it[i] in GLOBAL_FLAGS:
            i += 2
        else:
            break
    rest = it[i:]
    return rest[2] if len(rest) >= 3 and rest[:2] == ["check", "run"] else None


def match(command: str, cwd: Path, load: Any = config.load) -> Match | None:
    """The declared check this Bash command ran, if any (see the module docstring for the rule)."""
    ws = words(command)
    if not ws:
        return None
    segs, ops = segments(ws)
    where = cwd
    if len(segs) > 1 and len(segs[0]) == 2 and segs[0][0] == "cd" and ops[0] == "&&":
        where = (cwd / os.path.expanduser(segs[0][1])).resolve()
        rest, rest_ops = segs[1:], ops[1:]
    else:
        rest, rest_ops = segs, ops
    cfg = load(where)
    if not cfg.checks:
        return None
    for seg in segs:
        if (name := wrapped_check(seg)) is not None and name in cfg.checks:
            return Match(name, "wrapped", where, cfg.project, "benchbook check run records it")
    declared = declare(cfg.checks)
    for d in declared:
        if _equal(d, rest, rest_ops):
            return Match(d.name, "direct", where, cfg.project, junit=cfg.checks[d.name].junit)
    for d in declared:
        if _contains(d, segs, ops):
            return Match(d.name, "unrecordable", where, cfg.project,
                         "inside a pipeline or list: the exit code is not the check's")
    return None


# ---------------------------------------------------------------------------------------------- the result


def rough_signature(text: str) -> str | None:
    """The hook's signature input, from the Bash error text alone (signature.detail): the TypeScript errors when
    there are any, else a hash of the normalised last error line. checks.failure_signature turns it and the exit
    code into the attempt's signature at replay."""
    from .signature import detail

    return detail(text)


@dataclass
class Outcome:
    status: str | None  # ok | fail | timeout; None when the result cannot be read
    exit_code: int | None = None
    line_hash: str | None = None
    reason: str = ""
    shape_ok: bool = True  # False: the hook input is not shaped as its harness documents (harnesses.py, doctor)


def outcome(data: dict[str, Any]) -> Outcome:
    """What the Bash call's result says. Success fires PostToolUse; failure fires PostToolUseFailure, whose error
    starts with "Exit code N" (cc_hooks.md). A string tool_response on PostToolUse is a failure too (managed
    settings hooks can see those)."""
    ti = data.get("tool_input")
    if isinstance(ti, dict) and ti.get("run_in_background"):
        return Outcome(None, reason="run in the background: no result yet")
    if data.get("hook_event_name") == "PostToolUseFailure" or isinstance(data.get("tool_response"), str):
        if data.get("is_interrupt"):
            return Outcome(None, reason="interrupted")
        text = data.get("error") if data.get("hook_event_name") == "PostToolUseFailure" else data.get("tool_response")
        text = text if isinstance(text, str) else ""
        first, _, body = text.partition("\n")
        if "Command timed out" in text:
            return Outcome("timeout")
        m = _EXIT.match(first.strip())
        if m is None:
            return Outcome(None, reason="no exit code in the error")
        code = int(m.group(1))
        return Outcome("ok" if code == 0 else "fail", code, rough_signature(body) if code else None)
    resp = data.get("tool_response")
    if isinstance(resp, dict) and (resp.get("interrupted") or resp.get("backgroundTaskId")):
        return Outcome(None, reason="interrupted or moved to the background")
    return Outcome("ok", 0)


# ---------------------------------------------------------------------------------------------- the hooks


def _read_input() -> dict[str, Any]:
    raw = sys.stdin.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError(f"hook input exceeds {MAX_INPUT} characters")
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("hook input must be a JSON object")
    return data


def _log(msg: str) -> None:
    from .hooks import _log_notices, log

    log(msg)
    _log_notices()


def _observe(spool: Path, line: dict[str, Any]) -> None:
    """One line per declared check run the hook saw (the coverage metric's denominator)."""
    with (spool / OBSERVED).open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n")


def bash_result(harness: str = "claude-code") -> int:
    """harness: which harness runs the hook (`--harness` in its manifest); harnesses.py reads its input."""
    config.hold_notices()  # the harness runs this: nothing may reach stderr
    from .harnesses import answer, shell_call

    output = ""
    try:
        data = _read_input()
        if harness == "claude-code" and data.get("session_id"):
            from .session_notes import deliver

            event = "PostToolUseFailure" if data.get("hook_event_name") == "PostToolUseFailure" else "PostToolUse"
            if notes := deliver(data["session_id"]):
                output = json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": notes}},
                                    ensure_ascii=False) + "\n"
        call = shell_call(harness, data)
        if call is None:
            return 0
        found = match(call.command, call.cwd)
        if found is None:
            return 0
        _record(call, found)
    except Exception:  # noqa: BLE001 - a hook must not fail the tool call
        import traceback

        _log(f"{EVENT} failed ({harness}): " + traceback.format_exc())
    finally:
        sys.stdout.write(output or answer(harness))
    return 0


def _record(call: ShellCall, found: Match) -> None:
    from .check_run import find_repo, git_state, head_branch, resolve_piece, spool_attempt
    from .hooks import _spool_dir

    project = found.project
    res = call.outcome()
    repo = find_repo(found.cwd)
    piece, piece_from = resolve_piece(None, head_branch(repo) if repo else None)
    kind, reason = found.kind, found.reason
    spool = _spool_dir()
    spool.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = None
    if kind == "direct":
        if res.status is None:
            kind, reason = "unrecordable", res.reason
        elif found.junit and res.status == "ok":
            command = f"benchbook check run {shlex.quote(found.check)}"
            kind, reason = "unrecordable", f"JUnit evidence requires an invocation-specific report; run {command}"
        elif not project:
            kind, reason = "unrecordable", "no project"
        elif piece is None:
            kind, reason = "unrecordable", "no piece (HEAD is detached and BENCHBOOK_PIECE is unset)"
        elif repo is not None and found.cwd.resolve() != repo:
            kind, reason = "unrecordable", "not run from the repository root, where a declared check runs"
        elif repo is None or (state := git_state(repo)) is None or state.sha is None:
            kind, reason = "unrecordable", "no commit at HEAD"
    line: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "project": project, "piece": piece, "check": found.check,
        "kind": kind, "exit_code": res.exit_code, "reason": reason or None, "session_id": call.session_id,
        "tool_use_id": call.tool_use_id, "harness": call.harness}
    if call.version:
        line["harness_version"] = call.version
    if not res.shape_ok:
        line["input_shape"] = "unexpected"  # doctor: the harness changed what its hook sends
    _observe(spool, line)
    if kind != "direct" or project is None or piece is None or state is None or state.sha is None:
        _log(f"{EVENT}: observed {found.check} ({kind}{': ' + reason if reason else ''})")
        return
    entry: dict[str, Any] = {
        "piece": piece, "piece_from": piece_from, "check": found.check, "status": res.status,
        "exit_code": res.exit_code, "duration_ms": call.duration_ms,
        "failed_tests": [], "last_line_hash": res.line_hash,
        "sha": state.sha, "dirty": state.dirty, "changed": len(state.changed),
        "repo": str(repo), "agent": None, "interrupted": None, "via": VIA, "session_id": call.session_id,
        "tool_use_id": call.tool_use_id, "agent_type": call.agent_type}
    if call.harness != "claude-code":
        entry["harness"] = call.harness
    spool_attempt(EVENT, project, entry)
    _log(f"{EVENT}: queued {found.check} {res.status} (exit {res.exit_code}) for piece {piece} at {state.sha[:12]}")


def _base_branches(cfg: config.LocalConfig) -> set[str]:
    base = cfg.guard_base or "origin/main"
    return {base.rsplit("/", 1)[-1], *BASE_BRANCHES}


def _queued_passes(project: str, piece: str, sha: str, junit_checks: set[str]) -> set[str]:
    """Checks with a passing clean run still waiting in the spool (check run returns before its processor has
    written the attempt)."""
    from .hooks import ATTEMPT_EVENTS, _spool_dir

    out: set[str] = set()
    for p in _spool_dir().glob("*.json"):
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
            d = e["data"]
            event = e.get("event")
            name = str(d["check"])
            if (event in ATTEMPT_EVENTS and e.get("project") == project and d.get("piece") == piece
                    and d.get("sha") == sha and d.get("status") == "ok" and not d.get("dirty")
                    and (event != EVENT or name not in junit_checks)):
                out.add(name)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return out


def task_completed() -> int:
    """Exit 2 (Claude Code keeps the task open, stderr goes to the model) or 0. Fails open."""
    config.hold_notices()
    try:
        return _gate(_read_input())
    except Exception as e:  # noqa: BLE001 - a broken benchbook must not hold every task open
        _log(f"task-completed: allowed without a check (gate error: {type(e).__name__}: {str(e)[:200]})")
        return 0


def _allow(why: str) -> int:
    _log(f"task-completed: allowed: {why}")
    return 0


def _gate(data: dict[str, Any]) -> int:
    from .check_run import find_repo, git_state, head_branch, resolve_piece

    cwd = Path(data.get("cwd") or os.getcwd())
    cfg = config.load(cwd)
    if not cfg.project:
        return _allow(f"no benchbook project for {cwd}")
    if not cfg.checks:
        return _allow(f"no declared checks in {cfg.path}")
    repo = find_repo(cwd)
    if repo is None:
        return _allow(f"{cwd} is not in a git repository")
    branch = head_branch(repo)
    piece, source = resolve_piece(None, branch)
    if piece is None:
        return _allow(f"no piece in {repo} (HEAD is detached and BENCHBOOK_PIECE is unset)")
    if source == "the branch" and piece in _base_branches(cfg):
        return _allow(f"{repo} is on the base branch {piece}, not a piece's branch")
    state = git_state(repo)
    if state is None or state.sha is None:
        return _allow(f"no commit at HEAD in {repo}")
    from .attempts import check_states
    from .packs import load_pack

    junit_checks = {name for name, spec in cfg.checks.items() if spec.junit}
    pack, passed = check_states(cfg.project, piece, state.sha, junit_checks)
    if pack is None:
        return _allow(f"project {cfg.project} is not in the database")
    if "completion" not in load_pack(pack).claim_kinds:
        return _allow(f"project {cfg.project} uses the {pack} pack, not the code pack")
    queued = _queued_passes(cfg.project, piece, state.sha, junit_checks)
    missing = [n for n in cfg.checks if not passed.get(n) and n not in queued]
    if not missing:
        return _allow(f"piece {piece} passes every declared check at {state.sha[:12]}")
    sys.stderr.write(block_message(piece, state.sha, repo, missing, passed, len(state.changed)))
    _log(f"task-completed: blocked piece {piece} at {state.sha[:12]}: no passing run of {', '.join(missing)}")
    return 2


def block_message(piece: str, sha: str, repo: Path, missing: list[str], passed: dict[str, bool], changed: int) -> str:
    what = ", ".join(f"{n} ({'its run failed' if n in passed else 'not run'})" for n in missing)
    cmd = " && ".join(f"benchbook check run {shlex.quote(n)} --piece {shlex.quote(piece)}" for n in missing)
    msg = (f"benchbook: piece {piece} has no passing run at HEAD {sha[:12]} of: {what}.\n"
           f"Run this from {repo}, then mark the task completed again:\n  {cmd}\n")
    if changed:
        msg += (f"The checkout has {changed} uncommitted path(s); a run counts only for a clean checkout of a commit, "
                "so commit (or remove) them first.\n")
    return msg


def main(event: str, harness: str | None = None) -> int:
    """harness: `--harness` from the manifest's command. The completion gate is Claude Code's alone (TaskCompleted)."""
    if event == "bash-result":
        return bash_result(harness or "claude-code")
    if event == "bash-start":
        return 0  # older Claude Code manifests still invoke this hook
    return task_completed()
