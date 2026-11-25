"""PreToolUse(Bash) in Claude Code: a lead's `git merge` of a commit whose automatic re-run has not reported waits for
the verdict, and a merge the verdict does not clear is held once (docs/code.md, "In a swarm").

The boundary pilot (2026-11-25): arm F's lead merged all four pieces before their verdicts, three of them before the
verdict existed; a re-run took 12-13 s. verify_requests.enqueue marks a reported commit pending in the session's notes
directory (pending/<sha>.json, with the names its verdict note can have); this hook, run only for `git merge`
commands (the manifest's `if`), waits up to WAIT seconds for that note. A verdict that passed cleanly lets the merge
go ahead and is handed to the lead with it; any other verdict, or none in time, denies the call once with the reason.
The next merge of the same commit goes through: the lead decides, and a person can always merge.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from . import session_notes

PENDING = "pending"
WAIT = 30.0  # seconds a merge waits for its verdict; the manifest's timeout is above it
POLL = 0.25
CONTROL = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&", "\n"}
# -S and --gpg-sign take optional keys in the same argument, so they never consume the next token.
VALUE_OPTIONS = {"-m", "-F", "-s", "-X", "--message", "--file", "--strategy", "--strategy-option", "--into-name",
                 "--cleanup"}
STOPPING = {"--abort", "--continue", "--quit", "--skip"}
_CLEAN_VERDICT = re.compile(
    r"benchbook: \S+ [0-9a-f]{7}: re-run passed \(\d+ checks, \d+\.\d+s\), tests not weakened, checked: .+"
)


def pending_dir(notes: Path) -> Path:
    return notes / PENDING


def mark(notes: Path, commit: str, piece: str, verdicts: list[str]) -> None:
    """Record the latest report's verdict names; duplicate hook events keep its original wait and hold state.

    Called under the notes lock. A report-specific environment failure belongs only to that report.
    """
    path = pending_dir(notes) / f"{commit}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    if isinstance(old, dict) and old.get("verdicts") == verdicts:
        return
    rec = {"piece": piece, "commit": commit, "verdicts": verdicts, "since": time.time()}
    session_notes.write_note(path, json.dumps(rec))


def merge_targets(command: str) -> list[tuple[str | None, str]]:
    """(git -C directory or None, ref) of every `git merge <ref>` in a shell command; none for --abort and the
    like. Best effort, like the manifest's `if`: a merge the parser misses is not held."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    out: list[tuple[str | None, str]] = []
    i = 0
    while i < len(tokens):
        if os.path.basename(tokens[i]) != "git":
            i += 1
            continue
        j, where = i + 1, None
        while j < len(tokens) and tokens[j].startswith("-") and tokens[j] not in CONTROL:
            if tokens[j] in ("-C", "-c") and j + 1 < len(tokens):
                where = tokens[j + 1] if tokens[j] == "-C" else where
                j += 2
            else:
                j += 1
        if j >= len(tokens) or tokens[j] != "merge":
            i = j
            continue
        refs: list[str] = []
        stop = False
        j += 1
        while j < len(tokens) and tokens[j] not in CONTROL:
            t = tokens[j]
            if t in STOPPING:
                stop = True
            elif t in VALUE_OPTIONS:
                j += 1
            elif not t.startswith("-"):
                refs.append(t)
            j += 1
        if not stop:
            out += [(where, r) for r in refs]
        i = j
    return out


def _resolve(repo: Path, ref: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "--end-of-options",
                            f"{ref}^{{commit}}"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = p.stdout.strip()
    return sha if p.returncode == 0 and len(sha) == 40 else None


def _verdict(notes: Path, names: list[str]) -> tuple[str, bool] | None:
    """(verdict line, whether it was still waiting in notes/) of the first verdict note that exists."""
    for name in names:
        for path, waiting in ((notes / name, True), (notes / session_notes.DELIVERED / name, False)):
            try:
                return path.read_text(encoding="utf-8").strip(), waiting
            except OSError:
                continue
    return None


def _take(notes: Path, name_list: list[str]) -> None:
    """Move a verdict note this hook hands over into delivered/, so the next tool result does not repeat it."""
    for name in name_list:
        if (notes / name).is_file():
            (notes / session_notes.DELIVERED).mkdir(exist_ok=True)
            try:
                os.replace(notes / name, notes / session_notes.DELIVERED / name)
            except OSError:
                pass


def clean(line: str) -> bool:
    return _CLEAN_VERDICT.fullmatch(line) is not None


def _wait_for_verdict(
    notes: Path, path: Path, deadline: float,
) -> tuple[dict[str, Any], tuple[str, bool] | None] | None:
    """Read the current report at every poll, consuming its marker under the same lock as a new report."""
    from .hooks import _lock

    while True:
        with _lock(notes, blocking=True):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(rec, dict):
                return None
            if rec.get("held"):
                path.unlink(missing_ok=True)
                return None

            names = [n for n in rec.get("verdicts") or [] if isinstance(n, str) and "/" not in n]
            if found := _verdict(notes, names):
                _take(notes, names)
                path.unlink(missing_ok=True)
                return rec, found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                rec["held"] = True
                session_notes.write_note(path, json.dumps(rec))
                return rec, None
        time.sleep(min(POLL, remaining))


def decide(data: dict[str, Any], wait: float = WAIT) -> dict[str, Any] | None:
    """The hook's answer for one PreToolUse(Bash) input: None (no opinion), additionalContext, or a deny."""
    if not session_notes.for_lead(data) or data.get("tool_name") not in (None, "Bash"):
        return None
    notes = session_notes.notes_dir(data.get("session_id"))
    tool_input = data.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if notes is None or not isinstance(command, str) or not pending_dir(notes).is_dir():
        return None
    cwd = Path(data["cwd"]) if isinstance(data.get("cwd"), str) else Path.cwd()
    given: list[str] = []
    deadline = time.monotonic() + wait
    for where, ref in merge_targets(command):
        commit = _resolve(cwd / where if where else cwd, ref)
        path = pending_dir(notes) / f"{commit}.json" if commit else None
        if path is None or not path.is_file():
            continue
        if (current := _wait_for_verdict(notes, path, deadline)) is None:
            continue
        rec, found = current
        piece = str(rec.get("piece") or ref)
        if found is None:
            since = int(time.time() - float(rec.get("since") or time.time()))
            return _deny(f"benchbook: held this merge once: the re-run of {piece} at {commit[:7] if commit else ref} "
                         f"has not reported ({since} s since its report, {wait:g} s waited here). Merge after its "
                         "verdict line; merging again goes through without waiting.")
        line, waiting = found
        if not clean(line):
            return _deny(f"{line}\nbenchbook: held this merge once because the re-run did not pass cleanly; a person "
                         "decides (benchbook decide <claim> verified|refuted --oracle human --as human), or merge "
                         "again to go ahead anyway.")
        if waiting:
            given.append(line)
    if given:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(given)}}
    return None


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}
