"""`benchbook check verify`: re-run a piece's declared checks on the commit its implementer reported, in a clean
worktree, and record the report and the re-run as a pair (spec §8, measurement stage: the false completion rate).

1. Resolve the commit.
2. Integrity, against the merge-base of the base ref and the commit: the changed files that match the guard globs,
   and whether the [checks] tables differ between the merge-base and the commit.
3. Run the declared checks of the merge-base (never the commit's own) in a detached worktree of the commit, each
   through the core runner with its timeout. The worktree is removed whatever happens, interruption included.
4. Record: the completion claim in the implementer's name (--reported-by; under a token, which is one agent, in the
   verifier's name with payload.reported_by), the attempt in the verifier's, and a supports or refutes link from
   the attempt to the claim.
5. Decide, only with lead authority (the verifier has the orchestrator or human role): verified (oracle rerun) when
   the pack's [verify] rule accepts the re-run, inconclusive when earlier attempts of the piece at the commit (the
   implementer's own above all) disagree with it, nothing when a guarded path or [checks] changed: a person decides.
6. Report one row per check; the CLI exits 0 only when every check passed.

The diff and the check output are never stored: the commit, the guarded paths it changed and the failure signature
(signature.py) are. A failed attempt also leaves a discarded-approach claim, and verify names earlier attempts of the
piece that failed the same way (failures.py). Nothing here imports the database layer at module level; the ledger is
passed in."""
from __future__ import annotations

import hashlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from . import config as localcfg
from . import failures, signature
from .config import CheckSpec, parse_checks, parse_guard
from .errors import BenchbookError, InvalidTransition
from .globs import matching
from .runner import Interrupted, _trap_signals, run_command

if TYPE_CHECKING:
    from .ledger import Ledger

# Where a commit keeps its declarations; .assay/ is the folder's name before the rename.
CONFIG_PATHS = (".benchbook/config.toml", ".assay/config.toml")
DEFAULT_BASE = "origin/main"
# Used when the merge-base declares no [guard] paths: a guard that is silently empty would report "untouched".
DEFAULT_GUARD_PATHS = ("tests/**", "**/conftest.py", ".benchbook/config.toml")
MAX_GUARD_FILES = 100  # stored in metrics; guard_touched is computed from all of them
MAX_REPORT_BYTES = 10_000_000
MAX_FAILED_SHOWN = 50  # failing test ids kept on a result for the table; the signature is made from all of them
PIECE = re.compile(r"\S{1,100}")
STATUSES = ("ok", "fail", "timeout", "not_run")


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


@dataclass
class Base:
    ref: str
    sha: str
    merge_base: str
    config_path: str
    checks: dict[str, CheckSpec]
    guard_paths: list[str]
    guard_declared: bool


@dataclass
class Integrity:
    guard_files: list[str]  # changed files (deleted ones included) that match a guard glob
    checks_changed: bool

    @property
    def guard_touched(self) -> bool:
        return bool(self.guard_files)


@dataclass
class Report:
    """What is kept of --report-file: a hash, a size and the declared check names it mentions. Not its text, which
    can hold anything from a long transcript to a pasted secret."""
    sha256: str
    bytes: int
    mentions: list[str] = field(default_factory=list)


@dataclass
class Verification:
    piece: str
    commit: str
    base: Base
    integrity: Integrity
    results: list[CheckResult]
    duration_ms: int = 0
    interrupted: str | None = None  # the signal's name when the run stopped before the last check
    approach: str | None = None  # config.approach: what the implementer says it tried (failures.py)

    @property
    def passed(self) -> int:
        return sum(r.status == "ok" for r in self.results)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def all_passed(self) -> bool:
        return self.interrupted is None and self.passed == self.total

    @property
    def run_status(self) -> str:
        """Whether the checks ran to the end. A failing check is a result, not a failed run."""
        return "ok" if self.interrupted is None else "error"

    def config(self) -> dict[str, Any]:
        c: dict[str, Any] = {"piece": self.piece, "checks": [r.name for r in self.results]}
        if self.approach:
            c["approach"] = self.approach  # in the pack's dedup_ignore: naming an approach does not make a run new
        return c

    def metrics(self) -> dict[str, Any]:
        return {
            "checks_passed": self.passed,
            "checks_total": self.total,
            "checks": {r.name: r.status for r in self.results},
            "failure_signature": failure_signature(self.results),
            "guard_touched": self.integrity.guard_touched,
            "guard_files": self.integrity.guard_files[:MAX_GUARD_FILES],
            "checks_changed": self.integrity.checks_changed,
        }

    def notes(self) -> str:
        n = (f"check verify: base {self.base.ref} {self.base.sha[:12]}, merge-base {self.base.merge_base[:12]}; "
             f"checks from {self.base.config_path} at the merge-base")
        if not self.base.guard_declared:
            n += f"; no [guard] paths there, default guard {', '.join(DEFAULT_GUARD_PATHS)}"
        if len(self.integrity.guard_files) > MAX_GUARD_FILES:
            n += f"; {len(self.integrity.guard_files)} guarded files changed, first {MAX_GUARD_FILES} kept"
        if self.interrupted:
            done = sum(r.status != "not_run" for r in self.results)
            n += f"; interrupted ({self.interrupted}) after {done} of {self.total} checks"
        return n


@dataclass
class Outcome:
    """Step 5: what verify decided about the completion, and why, in one line."""
    decision: str | None  # verified | inconclusive | None (nothing decided)
    reason: str
    evidence: list[UUID] = field(default_factory=list)  # the attempt ids the decision cites (or would cite)
    decision_ids: list[UUID] = field(default_factory=list)  # supported then verified, or inconclusive
    person: bool = False  # a person has to decide: guarded files or [checks] changed, or the rule refused
    guard_files: list[str] = field(default_factory=list)
    checks_changed: bool = False


@dataclass
class Recorded:
    experiment_id: UUID
    verifier: str
    claim_id: UUID | None = None
    claim_existed: bool = False  # the same piece and commit had been reported before; evidence went to that claim
    reported_by: str | None = None
    relation: str | None = None  # supports | refutes; None when the run was interrupted
    duplicate_of: UUID | None = None  # an earlier verification of the same piece, checks and commit
    notice: str | None = None
    outcome: Outcome | None = None
    seen_before: list[failures.Prior] = field(default_factory=list)  # earlier attempts that failed the same way
    discarded_id: UUID | None = None  # the discarded-approach claim this failed attempt is evidence for
    discarded_new: bool = False
    discarded_error: str | None = None  # why that claim could not be written (the attempt is recorded)


# ---------------------------------------------------------------------------------------------------- git

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise BenchbookError(f"git {' '.join(args)}: {p.stderr.strip() or f'exit {p.returncode}'}")
    return p


def toplevel(path: Path) -> Path:
    p = _git(path, "rev-parse", "--show-toplevel", check=False)
    if p.returncode != 0:
        raise BenchbookError(f"not inside a git repository: {path}")
    return Path(p.stdout.strip())


def resolve_commit(repo: Path, ref: str) -> str:
    """Step 1: the full sha of a commit-ish."""
    p = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", check=False)
    if p.returncode != 0 or not p.stdout.strip():
        raise BenchbookError(f"not a commit in {repo}: {ref!r}")
    return p.stdout.strip()


class _Unreadable(Exception):
    pass


def config_at(repo: Path, sha: str) -> tuple[dict[str, Any], str] | None:
    """The committed config.toml of a commit and its path, None when it has none. _Unreadable: not valid TOML."""
    for rel in CONFIG_PATHS:
        p = _git(repo, "show", f"{sha}:{rel}", check=False)
        if p.returncode == 0:
            try:
                return tomllib.loads(p.stdout), rel
            except tomllib.TOMLDecodeError as e:
                raise _Unreadable(f"{rel} at {sha[:12]}: {e}") from e
    return None


def changed_paths(repo: Path, merge_base: str, commit: str) -> list[str]:
    """Files that differ between the merge-base and the commit. --no-renames: a test moved out of a guarded folder
    must show up as the deletion it is, not only under its new name."""
    out = _git(repo, "diff", "--name-only", "--no-renames", "-z", merge_base, commit).stdout
    return [p for p in out.split("\0") if p]


def read_base(repo: Path, ref: str, commit: str) -> Base:
    sha = resolve_commit(repo, ref)
    mb = _git(repo, "merge-base", sha, commit, check=False).stdout.strip()
    if not mb:
        raise BenchbookError(f"{commit[:12]} has no common ancestor with the base {ref}")
    try:
        found = config_at(repo, mb)
    except _Unreadable as e:
        raise BenchbookError(f"the base's declarations are unreadable: {e}") from e
    if found is None:
        raise BenchbookError(f"no .benchbook/config.toml at the merge-base {mb[:12]} of {ref} and {commit[:12]}: "
                             f"commit the [checks] declarations to {ref} first")
    data, rel = found
    where = f"{rel} at {mb[:12]}"
    checks = parse_checks(data.get("checks"), where)
    if not checks:
        raise BenchbookError(f"{where} declares no [checks.<name>]: commit the declarations to {ref} first")
    paths, _ = parse_guard(data.get("guard"), where)
    return Base(ref, sha, mb, rel, checks, paths or list(DEFAULT_GUARD_PATHS), bool(paths))


def integrity(repo: Path, base: Base, commit: str) -> Integrity:
    """Step 2."""
    guard_files = matching(changed_paths(repo, base.merge_base, commit), base.guard_paths)
    try:
        found = config_at(repo, commit)
    except _Unreadable:
        return Integrity(guard_files, True)
    before = config_at(repo, base.merge_base)
    old = before[0].get("checks") if before else None
    new = found[0].get("checks") if found else None
    return Integrity(guard_files, old != new)  # parsed tables: comments and layout do not count as a change


# ---------------------------------------------------------------------------------------------------- running

def run_checks(repo: Path, commit: str, specs: Sequence[CheckSpec]) -> tuple[list[CheckResult], BaseException | None]:
    """Step 3. Returns the results and, when the run was interrupted (Ctrl-C, SIGTERM, SIGHUP), the interruption
    for the caller to re-raise after recording. The worktree and its git metadata are gone when this returns."""
    results = [CheckResult(s.name, "not_run") for s in specs]
    tmp = Path(tempfile.mkdtemp(prefix="benchbook-verify-"))
    wt, out = tmp / "worktree", tmp / "out"  # out/ holds logs and JUnit files, outside the worktree
    out.mkdir()
    interrupted: BaseException | None = None
    previous = _trap_signals()  # a SIGTERM between two checks must reach the finally below too
    try:
        try:
            _git(repo, "worktree", "add", "--detach", "--quiet", str(wt), commit)
            for i, spec in enumerate(specs):
                results[i] = _run_one(spec, wt, out)
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


def _remove_worktree(repo: Path, wt: Path) -> None:
    p = _git(repo, "worktree", "remove", "--force", "--force", str(wt), check=False)
    if p.returncode != 0:  # never registered, or half created by an interrupted `worktree add`
        shutil.rmtree(wt, ignore_errors=True)
        _git(repo, "worktree", "prune", check=False)


def _run_one(spec: CheckSpec, wt: Path, out: Path) -> CheckResult:
    junit = out / f"{spec.name}.junit.xml"
    cmd = spec.cmd.replace("{junit}", shlex.quote(str(junit)))
    log = out / f"{spec.name}.log"
    r = run_command(cmd, wt, log, spec.timeout, metric=None, tee=False)
    status = {"ok": "ok", "timeout": "timeout"}.get(r.run_status, "fail")
    failed = signature.junit_failures(junit) if spec.junit and status != "ok" and junit.is_file() else []
    detail = signature.detail(signature.read_log(log), wt) if status != "ok" else None
    return CheckResult(spec.name, status, r.exit_code, r.duration_ms, failed[:MAX_FAILED_SHOWN], detail, r.log_tail,
                       signature.check_part(status, r.exit_code, failed, detail))


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


# ---------------------------------------------------------------------------------------------------- the command

def default_verifier() -> str:
    return f"verifier@{socket.gethostname() or 'localhost'}"[:100]


def read_report(path: Path, declared: Sequence[str]) -> Report:
    try:
        size = path.stat().st_size
        if size > MAX_REPORT_BYTES:
            raise BenchbookError(f"--report-file: {size} bytes, limit {MAX_REPORT_BYTES}")
        data = path.read_bytes()
    except OSError as e:
        raise BenchbookError(f"--report-file: {e}") from e
    text = data.decode("utf-8", errors="replace")
    mentions = [n for n in declared if re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w.-])", text)]
    return Report(hashlib.sha256(data).hexdigest(), len(data), mentions)


def verify(lg: Ledger, piece: str, commit: str, *, repo: Path, base: str | None = None,
           only: Sequence[str] | None = None, reported_by: str | None = None,
           report_file: Path | None = None) -> tuple[Verification, Recorded]:
    """Steps 1 to 4 (the CLI prints step 5). `lg` acts as the verifier. Raises the interruption, after cleaning up
    and recording, when the run was interrupted."""
    if not PIECE.fullmatch(piece):
        raise BenchbookError("piece: 1-100 characters without spaces")
    if "completion" not in lg.pack.claim_kinds:
        raise BenchbookError(f"check verify records completions: the project needs the code pack "
                             f"(benchbook init --pack code); {lg.project_id} uses {lg.pack.name}")
    if reported_by is not None and reported_by == lg.agent:
        raise BenchbookError(f"the verifier ({lg.agent}) cannot verify its own report: run as another agent "
                             "(--agent NAME, BENCHBOOK_AGENT) or leave both unset for verifier@<host>")
    if reported_by is not None and not 1 <= len(reported_by) <= 100:
        raise BenchbookError("--reported-by: 1-100 characters")
    if report_file is not None and not report_file.is_file():
        raise BenchbookError(f"--report-file: no such file: {report_file}")
    repo = toplevel(repo)
    sha = resolve_commit(repo, commit)
    b = read_base(repo, base or localcfg.load(repo).guard_base or DEFAULT_BASE, sha)
    names = list(b.checks) if not only else list(dict.fromkeys(only))
    if unknown := [n for n in names if n not in b.checks]:
        raise BenchbookError(f"--checks: {', '.join(unknown)} not declared at the merge-base "
                             f"(declared: {', '.join(b.checks)})")
    report = None if report_file is None else read_report(report_file, list(b.checks))
    integ = integrity(repo, b, sha)
    t0 = time.monotonic()
    results, interrupted = run_checks(repo, sha, [b.checks[n] for n in names])
    v = Verification(piece, sha, b, integ, results, int((time.monotonic() - t0) * 1000), _signal_name(interrupted))
    sig = failure_signature(results) if interrupted is None else None
    seen = failures.earlier(lg, piece, sig) if sig else []  # before recording: this run is not its own match
    if interrupted is not None:
        try:
            _record(lg, v, reported_by, report)
        except Exception as e:  # noqa: BLE001 - the interruption is what the caller must see
            interrupted.add_note(f"the interrupted attempt was not recorded: {e}")
        raise interrupted
    rec = _record(lg, v, reported_by, report)
    rec.seen_before = seen
    rec.outcome = _decide(lg, v, rec)
    return v, rec


def _signal_name(e: BaseException | None) -> str | None:
    if e is None:
        return None
    return signal.Signals(e.signum).name if isinstance(e, Interrupted) else "SIGINT"


def _record(lg: Ledger, v: Verification, reported_by: str | None, report: Report | None) -> Recorded:
    """Step 4. The claim is the implementer's (proposed, never decided here); the attempt and the link are the
    verifier's, so the ledger shows two different agents."""
    claim = None
    if reported_by is not None:
        payload: dict[str, Any] = {"piece": v.piece, "commit": v.commit,
                                   "reported_checks": report.mentions if report else []}
        if report is not None:
            payload["report"] = {"sha256": report.sha256, "bytes": report.bytes}
        proposer = lg
        if lg.token is None:
            proposer = lg.as_agent(reported_by)
        else:  # a token is one agent: the verifier records the claim and names the reporter (ledger.implementers)
            payload["reported_by"] = reported_by
        claim = proposer.propose_claim(
            "completion", f"{v.piece} is complete at {v.commit[:12]}", payload=payload,
            tags=[f"piece:{v.piece}", f"commit:{v.commit}"])
    r = lg.record_experiment(v.config(), v.metrics(), run_status=v.run_status, code_ref=v.commit, notes=v.notes(),
                             duration_ms=v.duration_ms)
    if r.id is None:  # only a pack with dedup_mode "refuse" can get here
        raise BenchbookError(f"the verification was refused as a duplicate of {r.duplicate_of}")
    rec = Recorded(r.id, lg.agent, duplicate_of=r.duplicate_of, notice=r.notice, reported_by=reported_by)
    if claim is not None:
        rec.claim_id, rec.claim_existed = claim.id, claim.duplicate_of is not None
        if v.run_status == "ok":
            rec.relation = "supports" if v.all_passed else "refutes"
            lg.link(r.id, rec.relation, claim.id)
    _discard(lg, v, rec)
    return rec


def _discard(lg: Ledger, v: Verification, rec: Recorded) -> None:
    """A finished attempt of the commit that failed leaves its discarded-approach claim (failures.py). A run with
    uncommitted changes is no attempt of its commit and leaves none; an interrupted one has no signature. The
    attempt is already recorded, so a failure here is reported, never raised: raising would make the spool record
    the attempt again."""
    sig = failure_signature(v.results)
    if v.run_status != "ok" or sig is None or getattr(v, "dirty", False):
        return
    try:
        c = failures.record_discarded(lg, rec.experiment_id, v.piece, sig, v.approach, v.commit)
    except Exception as e:  # noqa: BLE001 - see the docstring
        rec.discarded_error = f"{type(e).__name__}: {str(e).strip()[:200]}"
        return
    rec.discarded_id, rec.discarded_new = c.id, c.duplicate_of is None


def _decide(lg: Ledger, v: Verification, rec: Recorded) -> Outcome:
    """Step 5. The ledger's rule (decide, rule all_pass) is what makes a completion verified; this only picks the
    decision the evidence calls for and asks the rule before writing anything."""
    from .ids import short
    from .ledger import DECIDERS, TRANSITIONS

    if rec.claim_id is None:
        return Outcome(None, "no completion claim to decide (no --reported-by)")
    if lg.role not in DECIDERS:
        return Outcome(None, f"recorded without a decision: {lg.agent} has role {lg.role}, and verify decides only "
                             "with lead authority (orchestrator or human role)")
    info = lg.completion_attempts(rec.claim_id)
    assert info is not None
    status, claim, attempts = info["status"], rec.claim_id, info["attempts"]
    human = f"benchbook decide {short('claim', claim)} verified|refuted --oracle human --as human --rationale ..."
    if touched := [a for a in attempts if a["guard_touched"] is True or a["checks_changed"] is True]:
        files = sorted({f for a in touched for f in a["guard_files"]})
        changed = any(a["checks_changed"] is True for a in touched)
        what = " and ".join(([f"{len(files)} guarded file(s)"] if files else []) + (["[checks]"] if changed else []))
        return Outcome(None, f"no decision: the commit changed {what or 'a guarded path'}, so a re-run cannot verify "
                             f"it; a person has to decide ({human})", [a["id"] for a in touched], person=True,
                       guard_files=files, checks_changed=changed)
    mine = next((a for a in attempts if a["id"] == rec.experiment_id), None)
    if mine is None:  # the claim names another piece or commit than this run (an older claim with the same text)
        return Outcome(None, f"no decision: {short('claim', claim)} is about {info['piece']} at "
                             f"{str(info['commit'])[:12]}, not this run", person=True)
    others = [a for a in attempts if a["id"] != rec.experiment_id]
    if differ := [a for a in others if a["passed"] != mine["passed"]]:
        evidence = [rec.experiment_id, *(a["id"] for a in differ)]
        ids = ", ".join(f"{short('experiment', a['id'])} by {a['author']}" for a in differ)
        said = "passed" if mine["passed"] else "failed"
        if "inconclusive" not in TRANSITIONS.get(status, set()):
            return Outcome(None, f"no decision: this re-run {said} and {ids} did not, but the claim is already "
                                 f"{status}; a person decides ({human})", evidence, person=True)
        d = lg.decide(claim, "inconclusive", "rerun",
                      f"check verify: the re-run of {v.piece} at {v.commit[:12]} {said}, {ids} did not", evidence)
        return Outcome("inconclusive", f"inconclusive: the re-run {said} and {ids} did not", evidence, [d.id])
    if not mine["passed"]:
        return Outcome(None, "no decision: a check failed, and the refutes link records it (refuted is final, so a "
                             "person decides it; or the implementer reports a fixed commit)", [rec.experiment_id])
    evidence = [rec.experiment_id, *(a["id"] for a in others)]
    if status == "verified":
        return Outcome(None, "already verified; this re-run agrees", evidence)
    if status not in ("proposed", "inconclusive", "supported"):
        return Outcome(None, f"no decision: the claim is {status}", evidence)
    try:
        detail = lg.check_rerun(claim, evidence)
    except InvalidTransition as e:
        return Outcome(None, f"no decision: {e}", evidence, person=True)
    why = (f"check verify: every declared check passed in a re-run of {v.piece} at {v.commit[:12]} by {lg.agent}, "
           f"nothing guarded touched, [checks] unchanged; implementer {', '.join(detail['implementers'])}")
    done = []
    if status != "supported":
        done.append(lg.decide(claim, "supported", "rerun", why, evidence).id)
    try:
        done.append(lg.decide(claim, "verified", "rerun", why, evidence).id)
    except InvalidTransition as e:  # something was recorded between the check and the decision
        return Outcome(None, f"supported, not verified: {e}", evidence, done, person=True)
    return Outcome("verified", f"verified (oracle rerun, {len(evidence)} attempt(s) as evidence)", evidence, done)
