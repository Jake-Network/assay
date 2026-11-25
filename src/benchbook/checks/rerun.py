"""Run declared checks on a commit in a detached worktree, without the ledger credentials, and sign what failed.
The worktree is removed whatever happens, interruption included."""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import tempfile
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CheckSpec, VerifyPrep
from . import signature
from .gitrepo import _git
from .runner import Interrupted, _trap_signals, run_command

MAX_FAILED_SHOWN = 50  # failing test ids kept on a result for the table; the signature is made from all of them
STATUSES = ("ok", "fail", "timeout", "not_run")
CREDENTIALS = frozenset([f"{prefix}_{name}" for prefix in ("BENCHBOOK", "ASSAY")
                         for name in ("TOKEN", "DATABASE_URL", "READER_PASSWORD")] + ["PGPASSWORD", "PGPASSFILE"])


@dataclass
class CheckResult:
    name: str
    status: str  # ok | fail | timeout | not_run (the run was interrupted before this check)
    exit_code: int | None = None
    duration_ms: int = 0
    failed_tests: list[tuple[str, str | None]] = field(default_factory=list)  # (test id, exception type), JUnit
    # signature.detail of the output: the hash of the normalised last error line, or the TypeScript errors. The line
    # itself is not kept. (The name is from before tsc parts; the spool entries use it too.)
    last_line_hash: str | None = None
    tail: str = ""  # the end of the output, shown to whoever runs verify and never recorded
    signature: str | None = None  # this check's part of the signature, made where the output was (signature.py)
    tests: int | None = None  # JUnit testcases that ran (junit = true, readable report); T04 compares the counts
    environment: bool = False  # it fails the same way at the merge-base (T03)


@dataclass
class Prepared:
    """What run_checks did to the worktree before the checks (T02): the names of the copied files, whether prepare
    ran, and why the preparation failed (an environment failure: no check runs)."""
    copied: list[str] = field(default_factory=list)
    prepare: bool = False
    error: str | None = None

    def metrics(self) -> dict[str, Any] | None:
        return {"copied": list(self.copied), "prepare": self.prepare} if self.copied or self.prepare else None


# ---------------------------------------------------------------------------------------------------- running

def run_checks(repo: Path, commit: str, specs: Sequence[CheckSpec], env: dict[str, str] | None = None,
               prep: VerifyPrep | None = None,
               prepared: Prepared | None = None) -> tuple[list[CheckResult], BaseException | None]:
    """Step 3. Returns the results and, when the run was interrupted (Ctrl-C, SIGTERM, SIGHUP), the interruption
    for the caller to re-raise after recording. The worktree and its git metadata are gone when this returns. env:
    the checks' environment (None: this process's)."""
    results = [CheckResult(s.name, "not_run") for s in specs]
    tmp = Path(tempfile.mkdtemp(prefix="benchbook-verify-"))
    wt, out = tmp / "worktree", tmp / "out"  # out/ holds logs and JUnit files, outside the worktree
    out.mkdir()
    interrupted: BaseException | None = None
    previous = _trap_signals()  # a SIGTERM between two checks must reach the finally below too
    try:
        try:
            with _WORKTREES:
                _git(repo, "worktree", "add", "--detach", "--quiet", str(wt), commit)
            done = prepared if prepared is not None else Prepared()
            if prep is not None and (prep.copy or prep.prepare):
                _prepare(repo, commit, wt, out, prep, env, done)
            if done.error is None:
                for i, spec in enumerate(specs):
                    results[i] = _run_one(spec, wt, out, env)
        except (Interrupted, KeyboardInterrupt) as e:
            interrupted = e
        finally:
            with _signals_held():
                _remove_worktree(repo, wt)
                shutil.rmtree(tmp, ignore_errors=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return results, interrupted


def _prepare(repo: Path, commit: str, wt: Path, out: Path, prep: VerifyPrep, env: dict[str, str] | None,
             done: Prepared) -> None:
    """T02: copy prep.copy from the repository root into the worktree (regular files inside the repository that the
    commit does not track), then run prep.prepare there. A failure is written to done.error, and no check runs."""
    root = repo.resolve()
    for rel in prep.copy:
        src = repo / rel
        try:
            inside = src.resolve().is_relative_to(root)
        except OSError:
            inside = False
        if not inside:
            done.error = f"[verify] copy: {rel} is outside the repository"
            return
        if _git(repo, "ls-tree", "--name-only", commit, "--", rel, check=False).stdout.strip():
            done.error = f"[verify] copy: {rel} is tracked at {commit[:12]}; only untracked files are copied"
            return
        if src.is_symlink() or not src.is_file():
            done.error = f"[verify] copy: {rel} is not a regular file in {repo}"
            return
        dst = wt / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        done.copied.append(rel)
    if prep.prepare is not None:
        done.prepare = True
        r = run_command(prep.prepare, wt, out / "prepare.log", prep.prepare_timeout, metric=None, env=env, tee=False)
        if r.run_status != "ok":
            what = "timed out" if r.run_status == "timeout" else f"exit {r.exit_code}"
            done.error = f"[verify] prepare ({prep.prepare}) {what}"


@contextmanager
def _signals_held() -> Iterator[None]:
    """A second Ctrl-C or SIGTERM during cleanup waits until the worktree is gone."""
    sigs = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    try:
        old = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
    except (AttributeError, OSError, ValueError):  # no pthread_sigmask on this platform
        yield
        return
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


# git worktree add, remove and prune of one process, one at a time: with [verify] parallel_baseline two runs of the
# checks add and remove their worktrees at once, and both are named "worktree" under their own temporary folders.
_WORKTREES = threading.Lock()


def _remove_worktree(repo: Path, wt: Path) -> None:
    with _WORKTREES:
        p = _git(repo, "worktree", "remove", "--force", "--force", str(wt), check=False)
        if p.returncode != 0:  # never registered, or half created by an interrupted `worktree add`
            shutil.rmtree(wt, ignore_errors=True)
            _git(repo, "worktree", "prune", check=False)


def _run_one(spec: CheckSpec, wt: Path, out: Path, env: dict[str, str] | None = None) -> CheckResult:
    junit = out / f"{spec.name}.junit.xml"
    cmd = spec.cmd.replace("{junit}", shlex.quote(str(junit)))
    log = out / f"{spec.name}.log"
    r = run_command(cmd, wt, log, spec.timeout, metric=None, env=env, tee=False)
    status = {"ok": "ok", "timeout": "timeout"}.get(r.run_status, "fail")
    failed = signature.junit_failures(junit) if spec.junit and status != "ok" and junit.is_file() else []
    part = None
    if spec.junit and status == "ok":  # exit 0 passes only with a report in which a test ran and none failed
        part, failed = signature.junit_exit0(junit)
        status = "ok" if part is None else "fail"
    detail = signature.detail(signature.read_log(log), wt) if status != "ok" else None
    found = signature.junit_read(junit) if spec.junit and junit.is_file() else None
    return CheckResult(spec.name, status, r.exit_code, r.duration_ms, failed[:MAX_FAILED_SHOWN], detail, r.log_tail,
                       part or signature.check_part(status, r.exit_code, failed, detail),
                       found[0] if found else None)


def check_env() -> dict[str, str]:
    """The environment check verify runs the checks with: this process's, without CREDENTIALS. Everything else
    (PATH, the checks' own variables) is kept."""
    return {k: v for k, v in os.environ.items() if k not in CREDENTIALS}


def failed_tests(path: Path, limit: int = MAX_FAILED_SHOWN) -> list[tuple[str, str | None]]:
    """(test id, exception type) of the failing or erroring testcases of a JUnit XML file, sorted by test id."""
    return signature.junit_failures(path)[:limit]


def last_line_hash(text: str) -> str | None:
    """The hash of the normalised last error line of an output (signature.line_hash)."""
    return signature.line_hash(text)


def failure_signature(results: Sequence[CheckResult]) -> str | None:
    """The attempt's signature (signature.py): `check:part` per failing check, in run order. A result made where
    the output was carries its part; one rebuilt from a spool entry is signed from its status, exit code, failing
    tests and output detail."""
    return signature.combine((r.name, r.signature or signature.check_part(r.status, r.exit_code, r.failed_tests,
                                                                          r.last_line_hash)) for r in results)
