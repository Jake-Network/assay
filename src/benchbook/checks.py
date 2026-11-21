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
import os
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
from uuid import UUID, uuid4

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
# The ledger credentials a verifier may hold (integrations/github-action exports the first two), under both prefixes,
# and libpq's own password sources. check verify runs the commit's code without them: the code under test is the
# implementer's, and must not write to the ledger as the verifier.
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
    # Every check declared at the merge-base, whether this run ran it or not (--checks runs some). Stored as
    # metrics.checks_declared: a run that left one out is partial, and the all_pass rule never counts it as evidence.
    declared: list[str] = field(default_factory=list)

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
    def missing(self) -> list[str]:
        """Declared checks this run did not run (--checks)."""
        ran = {r.name for r in self.results}
        return [n for n in self.declared if n not in ran]

    @property
    def complete(self) -> bool:
        """Every declared check ran: only such a run can verify a completion (ledger.attempt_summary)."""
        return bool(self.declared) and not self.missing

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
            "checks_declared": list(self.declared),
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
        if self.declared and self.missing:
            n += (f"; partial: {self.total} of {len(self.declared)} declared checks run (not run: "
                  f"{', '.join(self.missing)}), so this run cannot verify the completion")
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

def run_checks(repo: Path, commit: str, specs: Sequence[CheckSpec],
               env: dict[str, str] | None = None) -> tuple[list[CheckResult], BaseException | None]:
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
            _git(repo, "worktree", "add", "--detach", "--quiet", str(wt), commit)
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
    return CheckResult(spec.name, status, r.exit_code, r.duration_ms, failed[:MAX_FAILED_SHOWN], detail, r.log_tail,
                       part or signature.check_part(status, r.exit_code, failed, detail))


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
           report_file: Path | None = None, trace_ref: dict[str, Any] | None = None,
           decide: bool = True, claim: str | None = None) -> tuple[Verification, Recorded]:
    """Steps 1 to 4 (the CLI prints step 5). `lg` acts as the verifier. Raises the interruption, after cleaning up
    and recording, when the run was interrupted. trace_ref is stored on the attempt as given (a CI run's URL, say);
    decide=False records without deciding, even with lead authority. claim: the completion claim the run is evidence
    for (what `pr pieces --claims` found); it must be about this piece at this commit, and its reporter is the
    implementer (reported_by, when also given, must name the same agent)."""
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
    given = None
    if claim is not None:
        given = given_claim(lg, claim, piece, sha, reported_by)
        reported_by = given[1]
        if reported_by == lg.agent:
            raise BenchbookError(f"the verifier ({lg.agent}) cannot verify its own report: --claim "
                                 f"{claim} was reported by {reported_by}")
    b = read_base(repo, base or localcfg.load(repo).guard_base or DEFAULT_BASE, sha)
    names = list(b.checks) if not only else list(dict.fromkeys(only))
    if unknown := [n for n in names if n not in b.checks]:
        raise BenchbookError(f"--checks: {', '.join(unknown)} not declared at the merge-base "
                             f"(declared: {', '.join(b.checks)})")
    report = None if report_file is None else read_report(report_file, list(b.checks))
    integ = integrity(repo, b, sha)
    t0 = time.monotonic()
    results, interrupted = run_checks(repo, sha, [b.checks[n] for n in names], check_env())
    v = Verification(piece, sha, b, integ, results, int((time.monotonic() - t0) * 1000), _signal_name(interrupted),
                     declared=list(b.checks))
    found = None if given is None else given[0]
    sig = failure_signature(results) if interrupted is None else None
    seen = failures.earlier(lg, piece, sig) if sig else []  # before recording: this run is not its own match
    if interrupted is not None:
        try:
            _record(lg, v, reported_by, report, trace_ref, found)
        except Exception as e:  # noqa: BLE001 - the interruption is what the caller must see
            interrupted.add_note(f"the interrupted attempt was not recorded: {e}")
        raise interrupted
    rec = _record(lg, v, reported_by, report, trace_ref, found)
    rec.seen_before = seen
    rec.outcome = _decide(lg, v, rec) if decide else Outcome(None, "recorded without a decision (--no-decide)")
    return v, rec


def _signal_name(e: BaseException | None) -> str | None:
    if e is None:
        return None
    return signal.Signals(e.signum).name if isinstance(e, Interrupted) else "SIGINT"


def _record(lg: Ledger, v: Verification, reported_by: str | None, report: Report | None,
            trace_ref: dict[str, Any] | None = None, given: UUID | None = None) -> Recorded:
    """Step 4. The claim is the implementer's (proposed, never decided here); the attempt and the link are the
    verifier's, so the ledger shows two different agents. given: a claim already checked by given_claim."""
    claim: tuple[UUID, bool] | None = None
    if given is not None:
        claim = (given, True)
    elif reported_by is not None:
        claim = _completion_claim(lg, v, reported_by, report)
    r = lg.record_experiment(v.config(), v.metrics(), run_status=v.run_status, code_ref=v.commit, notes=v.notes(),
                             duration_ms=v.duration_ms, trace_ref=trace_ref)
    if r.id is None:  # only a pack with dedup_mode "refuse" can get here
        raise BenchbookError(f"the verification was refused as a duplicate of {r.duplicate_of}")
    rec = Recorded(r.id, lg.agent, duplicate_of=r.duplicate_of, notice=r.notice, reported_by=reported_by)
    if claim is not None:
        rec.claim_id, rec.claim_existed = claim
        # A failure refutes whatever else was run. A pass supports only when every declared check ran: a partial
        # pass is recorded beside the claim, unlinked, so it neither verifies nor counts as a re-run in the false
        # completion rate (docs/code.md, "The numbers").
        if v.run_status == "ok" and (not v.all_passed or v.complete):
            rec.relation = "supports" if v.all_passed else "refutes"
            lg.link(r.id, rec.relation, rec.claim_id)
    _discard(lg, v, rec)
    return rec


def statements(piece: str, commit: str, reporter: str) -> tuple[str, str]:
    """The two statements a completion claim of check verify can have: the usual one, and the one naming the reporter
    that is used when someone else's claim already holds the usual one."""
    usual = f"{piece} is complete at {commit[:12]}"
    return usual, f"{usual}, reported by {reporter}"


# The completion claims of a piece and commit, oldest first: tagged with both and naming both in the payload, as
# pr.py finds them. Whatever their statement: a report written in other words is the same report (the reporter
# decides, in _completion_claim).
_OWN_CLAIMS = ("SELECT c.id, c.author, c.payload, a.role AS author_role FROM benchbook.claims c"
               " LEFT JOIN benchbook.agents a ON a.project_id = c.project_id AND a.id = c.author"
               " WHERE c.project_id = %s AND c.kind = 'completion' AND c.tags @> %s"
               " AND c.payload ->> 'piece' = %s AND c.payload ->> 'commit' = %s"
               " ORDER BY c.created_at, c.id")
_CLAIM_ROW = ("SELECT c.kind, c.author, c.tags, c.payload, a.role AS author_role FROM benchbook.claims c"
              " LEFT JOIN benchbook.agents a ON a.project_id = c.project_id AND a.id = c.author"
              " WHERE c.project_id = %s AND c.id = %s")


def given_claim(lg: Ledger, ref: str, piece: str, commit: str, reported_by: str | None) -> tuple[UUID, str]:
    """(claim id, reporter) of --claim, after checking that it is a completion claim of this project about piece at
    commit (tags and payload, as pr.py finds it), and that --reported-by, when given, names its reporter."""
    from .ids import short
    from .ledger import reporter_of

    _, cid = lg.resolve(ref, ["claim"])  # within lg's project: another project's claim is not found
    rows = lg.sql_internal(_CLAIM_ROW, (lg.project_id, cid))
    if not rows:
        raise BenchbookError(f"--claim {ref}: no such claim in {lg.project_id}")
    row, name = rows[0], short("claim", cid)
    if row["kind"] != "completion":
        raise BenchbookError(f"--claim {name} is a {row['kind']} claim, not a completion")
    payload = row["payload"] if isinstance(row["payload"], dict) else {}
    tags = set(row["tags"] or [])
    if (payload.get("piece") != piece or payload.get("commit") != commit
            or not {f"piece:{piece}", f"commit:{commit}"} <= tags):
        raise BenchbookError(f"--claim {name} is about {payload.get('piece')!r} at {str(payload.get('commit'))[:12]} "
                             f"(tags and payload), not {piece} at {commit[:12]}")
    reporter = reporter_of(str(row["author"]), row["author_role"], payload.get("reported_by"))
    if reported_by is not None and reported_by != reporter:
        raise BenchbookError(f"--reported-by {reported_by}, but {name} records the report of {reporter}")
    return cid, reporter


def _completion_claim(lg: Ledger, v: Verification, reporter: str, report: Report | None) -> tuple[UUID, bool]:
    """The completion claim of (piece, commit, reporter), and whether it existed. A claim's identity is that triple,
    not its statement: propose_claim deduplicates on statement_hash, which any agent can take first (and which the
    client computes), so a claim is reused only when it is about this piece and commit (tags and payload) and records
    this reporter's report: ledger.reporter_of names the reporter, or the proposer wrote it exactly as this call
    would. When another claim holds the usual statement, the claim names the reporter in its statement; when that
    one is taken too, it gets the same statement under a hash nobody can know in advance."""
    from .dedup import statement_hash
    from .ledger import reporter_of

    payload: dict[str, Any] = {"piece": v.piece, "commit": v.commit,
                               "reported_checks": report.mentions if report else []}
    if report is not None:
        payload["report"] = {"sha256": report.sha256, "bytes": report.bytes}
    proposer = lg
    if lg.token is None:
        proposer = lg.as_agent(reporter)
    else:  # a token is one agent: the verifier records the claim and names the reporter (ledger.implementers)
        payload["reported_by"] = reporter
    named = payload.get("reported_by")
    usual, with_reporter = statements(v.piece, v.commit, reporter)
    tags = [f"piece:{v.piece}", f"commit:{v.commit}"]

    def existing() -> UUID | None:
        for row in lg.sql_internal(_OWN_CLAIMS, (lg.project_id, tags, v.piece, v.commit)):
            by = (row["payload"] or {}).get("reported_by")
            if reporter_of(row["author"], row["author_role"], by) == reporter or (
                    row["author"] == proposer.agent and by == named):
                return row["id"]  # type: ignore[no-any-return]
        return None

    if (found := existing()) is not None:
        return found, True
    for statement, h in ((usual, None), (with_reporter, None),
                         (with_reporter, statement_hash(f"{with_reporter}\0{uuid4().hex}"))):
        c = proposer._propose_claim("completion", statement, payload, tags, _dedup_hash=h)
        if c.duplicate_of is None:
            return c.id, False
        if (found := existing()) is not None:  # a verify of the same report recorded it in the meantime
            return found, True
    raise BenchbookError("the completion claim could not be recorded")  # unreachable: the last hash is new


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
    if mine["passed"] and not mine["complete"]:
        return Outcome(None, f"no decision: this run ran {v.total} of the {len(v.declared)} declared checks "
                             f"(not run: {', '.join(v.missing)}); only a run of every declared check can verify",
                       [rec.experiment_id])
    others = [a for a in attempts if a["id"] != rec.experiment_id]
    # A failed run disagrees with a passing one that ran every declared check, not with one that skipped the check
    # that failed here (--checks).
    if differ := [a for a in others if a["passed"] != mine["passed"] and (mine["passed"] or a["complete"])]:
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
