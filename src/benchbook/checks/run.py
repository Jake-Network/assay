"""`benchbook check run <name>`: run one declared check from the current checkout and record it, so an
implementer's own runs reach the ledger without a second command (spec §7, layer 1: the wrapper).

The check runs through the core runner with tee: its output reaches stdout byte for byte, as `cmd 2>&1` would print
it, and the exit code is the command's. What the wrapper adds goes to stderr after the output: one receipt line.

Recording never waits for the database. The result is written to the hook spool (hooks._write_spool, one file per
database target) and the spool's detached processor records it, as the result-capture hook does. A database that is
down or slow therefore cannot delay the command, change its exit code or lose the result: the entry stays queued
and `benchbook hook replay` records it later. The processor records the attempt in check verify's shape
(attempts.record_spooled, through checks._record).

After a failure, and only then, the run is signed (signature.py) and one short, bounded lookup asks the ledger whether
an earlier attempt of the same piece failed the same way (failures.quick_notice); the answer is one more stderr line,
or nothing when the ledger does not answer in time. A passing run never touches the database.

This module is on the fast path (entry.py runs it without the CLI framework) and must not import the database layer,
typer or the checks module at import time: the time it adds to a check is bounded (300 ms, tests/test_check_run.py).
"""
from __future__ import annotations

import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..config import env

EVENT = "check-run"  # the spool entry's event; attempts.record_spooled records it
VIA = "check-run"  # metrics.via of the attempt it becomes
NOT_RUN = 2  # exit code when the check did not start (unknown check, no project, no piece): nothing is recorded
TIMEOUT_EXIT = 124  # as exp run and timeout(1)
PIECE = re.compile(r"\S{1,100}")  # checks.PIECE; repeated so this module stays light
APPROACH = PIECE  # failures.APPROACH: it becomes the tag approach:<name>
MAX_FAILED_SPOOLED = 50  # failing test ids queued with the run (for the receipt); the signature uses all of them
GIT_TIMEOUT = 10


class NotRun(Exception):  # noqa: N818 - a reason, shown to the user
    """The check was not started; the message says why and what to do."""


# ---------------------------------------------------------------------------------------------- the checkout


def find_repo(start: Path) -> Path | None:
    """The working tree root containing start: the nearest directory with a .git directory or file (a linked
    worktree or submodule has a file). Read from the file system, without running git."""
    cur = start.resolve()
    for d in (cur, *cur.parents):
        if (d / ".git").exists():
            return d
    return None


def git_dir(repo: Path) -> Path | None:
    dot = repo / ".git"
    if dot.is_dir():
        return dot
    try:
        line = dot.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    return (repo / line[len("gitdir:"):].strip()).resolve()


def head_branch(repo: Path) -> str | None:
    """The branch HEAD points at, None when HEAD is detached or unreadable."""
    gd = git_dir(repo)
    try:
        head = (gd / "HEAD").read_text(encoding="utf-8").strip() if gd else ""
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return (head[len(prefix):] or None) if head.startswith(prefix) else None


def resolve_piece(explicit: str | None, branch: str | None) -> tuple[str | None, str]:
    """The piece and where it came from: --piece, then BENCHBOOK_PIECE, then the branch name."""
    if explicit:
        return explicit, "--piece"
    if piece := env("PIECE"):
        return piece, "BENCHBOOK_PIECE"
    if branch:
        return branch, "the branch"
    return None, "nothing"


@dataclass
class GitState:
    sha: str | None  # HEAD's commit, None before the first commit
    branch: str | None
    changed: list[str] = field(default_factory=list)  # uncommitted paths, untracked ones included

    @property
    def dirty(self) -> bool:
        return bool(self.changed)


def start_git_state(repo: Path) -> subprocess.Popen[str] | None:
    """Ask git for HEAD, the branch and the uncommitted paths in one process. --no-optional-locks: it runs while
    the check runs and must not take the index lock a check's own git commands may need."""
    try:
        return subprocess.Popen(["git", "--no-optional-locks", "-C", str(repo), "status", "--porcelain=v2", "--branch",
                                 "-z"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None


def finish_git_state(proc: subprocess.Popen[str] | None) -> GitState | None:
    if proc is None:
        return None
    try:
        out, _ = proc.communicate(timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    return parse_status(out) if proc.returncode == 0 else None


def git_state(repo: Path) -> GitState | None:
    return finish_git_state(start_git_state(repo))


def parse_status(out: str) -> GitState:
    """`git status --porcelain=v2 --branch -z`: header lines start with '#'; every other record is a changed path.
    A rename record (2) is followed by its original path, which is skipped."""
    st = GitState(None, None)
    records = out.split("\0")
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if rec.startswith("# branch.oid "):
            oid = rec[len("# branch.oid "):]
            st.sha = None if oid == "(initial)" else oid
        elif rec.startswith("# branch.head "):
            head = rec[len("# branch.head "):]
            st.branch = None if head == "(detached)" else head
        elif rec and not rec.startswith("#"):
            fields = rec.split(" ")
            st.changed.append(fields[-1])
            if rec.startswith("2 "):
                i += 1
    return st


# ---------------------------------------------------------------------------------------------- the spool


def spool_attempt(event: str, project: str, data: dict[str, Any]) -> Path:
    """Queue one attempt for the ledger and make sure a processor drains the spool. Returns the spool directory."""
    from ..hooks.spooler import _spool_dir, _write_spool, start_processor

    spool = _spool_dir()
    spool.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    spool.mkdir(exist_ok=True, mode=0o700)
    _write_spool(spool, {"event": event, "project": project, "data": data, "spooled_at": time.time(),
                         "accepted": True})
    start_processor(spool)
    return spool


# ---------------------------------------------------------------------------------------------- the command


@dataclass
class Plan:
    name: str
    cmd: str
    junit: bool
    timeout: float
    project: str
    piece: str
    piece_from: str
    repo: Path
    agent: str | None
    approach: str | None = None


def plan(name: str, piece: str | None = None, *, project: str | None = None, agent: str | None = None,
         cwd: Path | None = None, approach: str | None = None) -> Plan:
    """Everything decided before the check starts. NotRun when it cannot be recorded."""
    from ..errors import BenchbookError

    here = (cwd or Path.cwd()).resolve()
    try:
        cfg = config.load(here)
    except (BenchbookError, ValueError, OSError) as e:  # tomllib.TOMLDecodeError is a ValueError
        raise NotRun(f"cannot read the declared checks: {e}") from e
    if cfg.path is None:
        raise NotRun(f"no .benchbook/config.toml in {here} or above: declare the check under [checks.{name}] first")
    spec = cfg.checks.get(name)
    if spec is None:
        declared = ", ".join(cfg.checks) or "none"
        raise NotRun(f"no check named {name!r} in {cfg.path} (declared: {declared})")
    project = project or cfg.project
    if not project:
        raise NotRun(f"no project in {cfg.path} and BENCHBOOK_PROJECT is unset")
    repo = find_repo(here)
    if repo is None:
        raise NotRun(f"not inside a git repository: {here}")
    chosen, source = resolve_piece(piece, head_branch(repo))
    if chosen is None:
        raise NotRun("no piece: HEAD is detached; pass --piece NAME or set BENCHBOOK_PIECE")
    if not PIECE.fullmatch(chosen):
        raise NotRun(f"piece {chosen!r} (from {source}): 1-100 characters without spaces")
    approach = approach or env("APPROACH") or None
    if approach is not None and not APPROACH.fullmatch(approach):
        raise NotRun(f"approach {approach!r} (--approach or BENCHBOOK_APPROACH): 1-100 characters without spaces")
    return Plan(name, spec.cmd, spec.junit, spec.timeout, project, chosen, source, repo, agent or env("AGENT"),
                approach)


def exit_code(exit: int | None, status: str, signum: int | None) -> int:
    if signum is not None:
        return 128 + signum
    if status == "timeout" or exit is None:
        return TIMEOUT_EXIT
    return 128 - exit if exit < 0 else exit  # a shell killed by signal N: -N from Popen, 128+N from a shell


def run(name: str, piece: str | None = None, approach: str | None = None, *, project: str | None = None,
        agent: str | None = None, cwd: Path | None = None) -> int:
    """The command. Returns the exit code to exit with: the check's own, 124 after its timeout, 128+N after signal N,
    or NOT_RUN (2) when it did not start."""
    try:
        p = plan(name, piece, project=project, agent=agent, cwd=cwd, approach=approach)
    except NotRun as e:
        print(f"benchbook: check {name} not run: {e}", file=sys.stderr)
        return NOT_RUN
    from ..core.runner import Interrupted, run_command

    tmp = Path(tempfile.mkdtemp(prefix="benchbook-check-run-"))
    junit = tmp / f"{p.name}.junit.xml"
    cmd = p.cmd.replace("{junit}", shlex.quote(str(junit)))
    git = start_git_state(p.repo)  # runs beside the check, so it adds nothing to the wait
    t0 = time.monotonic()
    signum: int | None = None
    try:
        try:
            res = run_command(cmd, p.repo, tmp / "out.log", p.timeout, metric=None, tee=True)
            status = {"ok": "ok", "timeout": "timeout"}.get(res.run_status, "fail")
            exit, duration = res.exit_code, res.duration_ms
        except (KeyboardInterrupt, Interrupted) as e:
            signum = e.signum if isinstance(e, Interrupted) else int(signal.SIGINT)
            status, exit, duration = "interrupted", None, int((time.monotonic() - t0) * 1000)
        state = finish_git_state(git)
        failed = _failed_tests(junit) if p.junit and status == "fail" and junit.is_file() else []
        problem = None
        if p.junit and status == "ok":  # as check verify: exit 0 passes only with a report in which a test ran
            problem, failed = _junit_exit0(junit)
            status = "ok" if problem is None else "fail"
        sig = _sign(status, exit, failed, tmp / "out.log", p.repo, problem) if status != "ok" else None
    finally:
        _remove(tmp)
    code = exit_code(exit, status, signum)
    _receipt(p, status, exit, duration, failed, state, signum, sig)
    return code


def _failed_tests(junit: Path) -> list[tuple[str, str | None]]:
    from .signature import junit_failures

    return junit_failures(junit)


def _junit_exit0(junit: Path) -> tuple[str | None, list[tuple[str, str | None]]]:
    from .signature import junit_exit0

    return junit_exit0(junit)


@dataclass
class Signed:
    detail: str | None  # signature.detail of the output (spool key last_line_hash)
    part: str | None  # this check's part of the attempt's signature; None when it did not fail (interrupted)


def _sign(status: str, exit: int | None, failed: list[tuple[str, str | None]], log: Path, repo: Path,
          problem: str | None = None) -> Signed:
    """The failure's signature, from the whole log (up to signature.MAX_LOG_BYTES of its end), not only the tail.
    problem: the part of a junit check that exited 0 without a passing report (signature.junit_exit0)."""
    from . import signature

    detail = signature.detail(signature.read_log(log), repo) if status in ("fail", "interrupted") else None
    return Signed(detail, problem or signature.check_part(status, exit, failed, detail))


def _remove(tmp: Path) -> None:
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _receipt(p: Plan, status: str, exit: int | None, duration: int, failed: list[tuple[str, str | None]],
             state: GitState | None, signum: int | None, sig: Signed | None) -> None:
    """Queue the result and say so on stderr. The command's output has already been written to stdout. After a
    failure, a second line names an earlier attempt of the piece that failed the same way, when the ledger
    answers in time (failures.quick_notice); the lookup runs before the run is queued, so it never finds itself."""
    if sys.stdout is not None:
        try:
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
    secs = f"{duration / 1000:.1f}s"
    what = f"exit {exit}" if exit is not None else (signal.Signals(signum).name if signum else "timed out")
    head = f"benchbook: {p.name} {status} ({what}, {secs})"
    if failed:
        shown = ", ".join(f"{t} ({k})" if k else t for t, k in failed[:3])
        head += f": failing {shown}" + (f" and {len(failed) - 3} more" if len(failed) > 3 else "")
    elif status == "fail" and exit == 0 and sig is not None and sig.part:
        head += f": {sig.part} (junit = true)"  # recorded as a failure; the exit code stays the command's
    if state is None or state.sha is None:
        why = "git could not report HEAD" if state is None else "the repository has no commit yet"
        print(f"{head}; not recorded: {why}", file=sys.stderr)
        return
    at = f"piece {p.piece} at {state.sha[:12]}"
    if state.dirty:
        at += f" with {len(state.changed)} uncommitted path(s), so not a run of that commit"
    data: dict[str, Any] = {
        "piece": p.piece, "piece_from": p.piece_from, "check": p.name, "status": status, "exit_code": exit,
        "duration_ms": duration, "failed_tests": [list(t) for t in failed[:MAX_FAILED_SPOOLED]],
        "last_line_hash": sig.detail if sig else None, "signature": sig.part if sig else None,
        "sha": state.sha, "dirty": state.dirty, "changed": len(state.changed), "repo": str(p.repo),
        "agent": p.agent, "interrupted": signal.Signals(signum).name if signum else None, "via": VIA,
        "definition": config.CheckSpec(p.name, p.cmd, p.junit, p.timeout).definition}
    if p.approach:
        data["approach"] = p.approach
    seen = None
    if sig is not None and sig.part is not None:
        from .failures import quick_notice
        from .signature import combine

        seen = quick_notice(p.project, p.piece, combine([(p.name, sig.part)]))
    try:
        spool = spool_attempt(EVENT, p.project, data)
    except Exception as e:  # noqa: BLE001 - the check ran; its exit code must survive a broken spool
        print(f"{head}, {at}; NOT recorded: the result could not be queued ({type(e).__name__}: {e})", file=sys.stderr)
    else:
        print(f"{head}, {at}; queued for the ledger ({spool})", file=sys.stderr)
    if seen:
        print(f"benchbook: {seen}", file=sys.stderr)


def parse_fast_args(args: list[str]) -> tuple[str, str | None, str | None] | None:
    """`check run NAME [--piece P] [--approach A]` for entry.py's fast path: (name, piece, approach). None for
    anything else (--help, other options, global options), which goes through the CLI and ends in the same run()."""
    name: str | None = None
    opts: dict[str, str | None] = {"--piece": None, "--approach": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a in opts and i + 1 < len(args):
            opts[a] = args[i + 1]
            i += 2
            continue
        flag, eq, value = a.partition("=")
        if eq and flag in opts:
            opts[flag] = value
        elif a.startswith("-") or name is not None:
            return None
        else:
            name = a
        i += 1
    return (name, opts["--piece"], opts["--approach"]) if name else None
