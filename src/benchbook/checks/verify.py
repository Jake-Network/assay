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
import signal
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4, uuid5

from .. import config as localcfg
from ..errors import BenchbookError, InvalidTransition
from . import failures, rerun
from .baseline import EarlyBaseline, classify_environment, count_tests, passed_elsewhere
from .declarations import DEFAULT_GUARD_PATHS, PIECE, Base, pin_changed, pinned, read_base
from .gitrepo import changed_paths, path_prefixes, resolve_commit, toplevel
from .guard import MAX_GUARD_FILES, Integrity, integrity
from .rerun import STATUSES, CheckResult, Prepared, _signals_held, check_env, failure_signature
from .runner import Interrupted, _trap_signals

if TYPE_CHECKING:
    from ..core.ledger import Ledger

DEFAULT_BASE = "origin/main"
MAX_REPORT_BYTES = 10_000_000


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
    # Why the run says nothing about the commit (T02, T03): the worktree could not be prepared, or every failing
    # check fails the same way at the merge-base. Such a run is an error: no link, no discarded approach.
    environment: str | None = None
    prepared: Prepared | None = None
    baseline_signature: str | None = None  # how the failing checks failed at the merge-base (T03)
    paths: list[str] = field(default_factory=list)  # directory prefixes the commit changed since the merge-base (T06)
    # the files themselves, at most MAX_FILES (none when more): what the brief's "changed since" compares (T10)
    files: list[str] = field(default_factory=list)
    # The failure signature of an attempt already in the ledger (a retried request finishing from the stored run):
    # its results keep only the statuses, so the signature is the recorded one.
    recorded_signature: str | None = None
    # verify(retry=True): RETRYING on a failed first run, RETRY_* on its retry (metrics.retry), which also names the
    # first run (metrics.retry_of) and how it failed (metrics.first_signature)
    retry: str | None = None
    retry_of: UUID | None = None
    first_signature: str | None = None

    def signature(self) -> str | None:
        return self.recorded_signature if self.recorded_signature is not None else failure_signature(self.results)

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
        return "ok" if self.interrupted is None and self.environment is None else "error"

    def config(self) -> dict[str, Any]:
        c: dict[str, Any] = {"piece": self.piece, "checks": [r.name for r in self.results]}
        if self.approach:
            c["approach"] = self.approach  # in the pack's dedup_ignore: naming an approach does not make a run new
        return c

    def metrics(self) -> dict[str, Any]:
        pin: dict[str, Any] = {}
        if self.base.source == "pin":  # a merge-base run keeps the keys it always had
            from ..core.ids import short

            pin = {"checks_source": "pin", "checks_sha256": self.base.checks_sha256 or "",
                   "checks_pin": short("claim", self.base.pin_id) if self.base.pin_id else ""}
        return {
            **pin,
            "checks_passed": self.passed,
            "checks_total": self.total,
            "checks": {r.name: r.status for r in self.results},
            "checks_declared": list(self.declared),
            "failure_signature": self.signature(),
            "guard_touched": self.integrity.guard_touched,
            "guard_files": self.integrity.guard_files[:MAX_GUARD_FILES],
            "checks_changed": self.integrity.checks_changed,
            "guard_weakened": self.integrity.guard_weakened,
            **({"guard_weakened_files": self.integrity.guard_weakened_files[:MAX_GUARD_FILES]}
               if self.integrity.guard_weakened_files else {}),
            **({"guard_added_files": (self.integrity.guard_added + self.integrity.guard_appended)[:MAX_GUARD_FILES]}
               if self.integrity.guard_added or self.integrity.guard_appended else {}),
            **({"tests_before": self.integrity.tests_before, "tests_after": self.integrity.tests_after}
               if self.integrity.tests_before is not None and self.integrity.tests_after is not None else {}),
            **({"fewer_tests_checks": list(self.integrity.fewer_tests_in)} if self.integrity.fewer_tests_in else {}),
            **({"prepared": prep} if self.prepared is not None and (prep := self.prepared.metrics()) else {}),
            **({"environment": True} if self.environment is not None else {}),
            **({"baseline_signature": self.baseline_signature} if self.baseline_signature else {}),
            **({"retry": self.retry} if self.retry else {}),
            **({"retry_of": str(self.retry_of)} if self.retry_of else {}),
            **({"first_signature": self.first_signature} if self.first_signature else {}),
        }

    def notes(self) -> str:
        n = (f"check verify: base {self.base.ref} {self.base.sha[:12]}, merge-base {self.base.merge_base[:12]}; "
             + (f"checks from {self.base.config_path} at the merge-base" if self.base.source != "pin"
                else f"checks from {self.base.config_path} (no config at the merge-base)"))
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
        if self.environment:
            n += f"; environment: {self.environment}; not counted against the piece"
        if self.retry:
            n += f"; {self.retry}"
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
# A completion's payload.files: more changed files than this and the brief compares its directory prefixes instead.
MAX_FILES = 200
# metrics.retry (verify(retry=True)): RETRYING on the failed first run, recorded before its retry runs; one of the
# others on the retry, which carries metrics.retry_of (the first run) and metrics.first_signature
RETRYING = "failed; retried"
RETRY_PASSED = "passed on a retry after a failed run"
RETRY_FAILED = "failed again on a retry"
RETRY_FAILED_OTHER = "failed again on a retry, differently"
RETRY_UNAVAILABLE = "retry could not run"


def implementers_of(lg: Ledger, piece: str, commit: str, reported_by: str | None) -> set[str]:
    """Who reported PIECE done at COMMIT: reported_by and the reporters of its completion claims so far."""
    from ..core.ledger import reporter_of

    out = {reported_by} if reported_by else set()
    for row in lg.sql_internal(_OWN_CLAIMS, (lg.project_id, [f"piece:{piece}", f"commit:{commit}"], piece, commit)):
        out.add(reporter_of(row["author"], row["author_role"], (row["payload"] or {}).get("reported_by")))
    return out


# ---------------------------------------------------------------------------------------------------- the command

def default_verifier() -> str:
    """The agent `check verify` runs as without --agent, BENCHBOOK_AGENT or a token (registered with the harness
    role: it records, a lead decides)."""
    return f"verifier@{socket.gethostname() or 'localhost'}"[:100]


def default_rerunner() -> str:
    """The agent the hook's automatic re-run (verify_requests) runs as without a token, registered with the
    orchestrator role so it decides. A separate name from default_verifier: whichever command registered a name
    first fixes its role in the project (Ledger._ensure_agent), so one shared name decided or not by run order."""
    return f"rerun@{socket.gethostname() or 'localhost'}"[:100]


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
           decide: bool = True, claim: str | None = None,
           receipt_id: UUID | None = None, retry: bool = False) -> tuple[Verification, Recorded]:
    """Steps 1 to 4 (the CLI prints step 5). `lg` acts as the verifier. Raises the interruption, after cleaning up
    and recording, when the run was interrupted. trace_ref is stored on the attempt as given (a CI run's URL, say);
    decide=False records without deciding, even with lead authority. claim: the completion claim the run is evidence
    for (what `pr pieces --claims` found); it must be about this piece at this commit, and its reporter is the
    implementer (reported_by, when also given, must name the same agent).

    retry: when every declared check ran and one failed (no environment failure, no weakened test), record that run
    without a discarded approach and run them all once more in a fresh worktree (_retry). The hook's automatic re-run
    retries (verify_requests.process); in the third boundary pilot two of its four re-runs failed once on socket
    timing tests outside the pieces, which became refutations and discarded approaches."""
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
    impl = implementers_of(lg, piece, sha, reported_by)
    b = read_base(repo, base or localcfg.load(repo).guard_base or DEFAULT_BASE, sha,
                  lambda: pinned(lg, piece, impl))
    names = list(b.checks) if not only else list(dict.fromkeys(only))
    if unknown := [n for n in names if n not in b.checks]:
        raise BenchbookError(f"--checks: {', '.join(unknown)} not declared at the merge-base "
                             f"(declared: {', '.join(b.checks)})")
    report = None if report_file is None else read_report(report_file, list(b.checks))
    moved = False
    if b.source == "pin":
        pin = pinned(lg, piece, impl)
        moved = pin is not None and pin_changed(lg, pin, piece, repo, impl)
    changed = changed_paths(repo, b.merge_base, sha)
    scope = path_prefixes(changed)
    files = changed if len(changed) <= MAX_FILES else []
    found = None if given is None else given[0]
    first_id = uuid5(receipt_id, "first run") if receipt_id is not None else None
    record_id = receipt_id
    stored = _stored_attempt(lg, receipt_id, piece, sha) if receipt_id is not None else None
    metrics = stored["metrics"] if stored is not None else None
    if isinstance(metrics, dict) and metrics.get("retry") == RETRY_UNAVAILABLE:
        # A terminal prepare failure leaves the original run as the check evidence.
        assert first_id is not None
        record_id = first_id
        stored = _stored_attempt(lg, first_id, piece, sha)
        if stored is None:
            raise BenchbookError(f"verification receipt {receipt_id} is missing its first run")
    elif stored is None and retry and first_id is not None:
        if first := _stored_attempt(lg, first_id, piece, sha):
            if _linked_discard(lg, first_id):
                # An older processor may have finished an unavailable retry before terminal receipts existed.
                stored, record_id = first, first_id
            else:
                v = _from_stored(first, piece, sha, b, scope)
                v.files = files
                return _retry(lg, v, repo, check_env(), reported_by, report, trace_ref, found,
                              first_id, receipt_id, decide)
    if stored is not None:
        # A retry of a request whose attempt is already in the ledger (the result cache was not written, a later
        # step failed): finish the links and the decision from that attempt, never from a new run, which could
        # disagree with what is stored.
        v = _from_stored(stored, piece, sha, b, scope)
        v.files = files
        sig = v.signature()
        seen = [p for p in failures.earlier(lg, piece, sig) if p.id != record_id] if sig else []
        rec = _record(lg, v, reported_by, report, trace_ref, found, receipt_id=record_id,
                      discard=record_id != receipt_id or v.retry in (None, RETRY_FAILED))
        rec.seen_before = seen
        rec.outcome = _decide(lg, v, rec) if decide else Outcome(None, "recorded without a decision (--no-decide)")
        return v, rec
    integ = integrity(repo, b, sha, moved)
    t0 = time.monotonic()
    prepared = Prepared()
    env = check_env()
    early = None
    trapped: dict[int, Any] = {}
    if b.prep.parallel_baseline and integ.guard_files and sha != b.merge_base:
        if junit := [b.checks[n] for n in names if b.checks[n].junit]:
            # SIGTERM and SIGHUP raise Interrupted for as long as the early run lives, not only while a check runs
            # (run_checks restores the handlers it found): a signal while waiting for it reaches early.stop().
            trapped = _trap_signals()
            early = EarlyBaseline(repo, b, junit, env)
    try:
        results, interrupted = rerun.run_checks(repo, sha, [b.checks[n] for n in names], env, b.prep, prepared)
        environment, base_sig = prepared.error, None
        if interrupted is None and environment is None and sha != b.merge_base:  # the base can't tell a commit apart
            environment, base_sig, interrupted = classify_environment(repo, b, results, env,
                                                                      passed_elsewhere(lg, piece, sha, b, impl))
        if interrupted is None and environment is None and integ.guard_files and sha != b.merge_base:
            interrupted = count_tests(repo, b, results, integ, env, early)
    finally:
        if early is not None:
            with _signals_held():
                early.stop()  # unused (an interruption, an environment failure, other counts) or already finished
        for signum, handler in trapped.items():
            signal.signal(signum, handler)
    v = Verification(piece, sha, b, integ, results, int((time.monotonic() - t0) * 1000), _signal_name(interrupted),
                     declared=list(b.checks), environment=environment, prepared=prepared, baseline_signature=base_sig,
                     paths=scope, files=files)
    sig = failure_signature(results) if interrupted is None else None
    seen = failures.earlier(lg, piece, sig) if sig else []  # before recording: this run is not its own match
    if (retry and interrupted is None and v.run_status == "ok" and v.complete and not v.all_passed
            and not integ.guard_weakened and not integ.checks_changed):
        return _retry(lg, v, repo, env, reported_by, report, trace_ref, found, first_id, receipt_id, decide, seen)
    if interrupted is not None:
        try:
            _record(lg, v, reported_by, report, trace_ref, found)
        except Exception as e:  # noqa: BLE001 - the interruption is what the caller must see
            interrupted.add_note(f"the interrupted attempt was not recorded: {e}")
        raise interrupted
    rec = _record(lg, v, reported_by, report, trace_ref, found, receipt_id=receipt_id)
    rec.seen_before = seen
    rec.outcome = _decide(lg, v, rec) if decide else Outcome(None, "recorded without a decision (--no-decide)")
    return v, rec


def _retry(lg: Ledger, v: Verification, repo: Path, env: dict[str, str], reported_by: str | None,
           report: Report | None, trace_ref: dict[str, Any] | None, found: UUID | None, first_id: UUID | None,
           receipt_id: UUID | None, decide: bool,
           seen: list[failures.Prior] | None = None) -> tuple[Verification, Recorded]:
    """verify(retry=True) after a complete run v failed. v is recorded first, under first_id (derived from the
    request's receipt, so a later try finds it and only retries) and without a discarded approach; then every
    declared check runs again in a fresh worktree and the retry is recorded under the receipt, naming v.

    - The retry passes: the runs disagree, and _decide makes the claim inconclusive. A lead who finds the failure
      is not the piece's invalidates v, and its own check verify then decides from the passing runs.
    - It fails the same way: the retry leaves the discarded approach v did not, and nothing is decided.
    - It fails differently: no discarded approach either, since neither failure repeated.
    - It could not run: record the failed preparation under the receipt, then finish v as the only check evidence.
    - It was interrupted: raise the interruption, so a later try of the request can resume."""
    v.retry = RETRYING
    first = _record(lg, v, reported_by, report, trace_ref, found, receipt_id=first_id, discard=False)
    prep = Prepared()
    t0 = time.monotonic()
    specs = [v.base.checks[n] for n in v.declared]
    again, interrupted = rerun.run_checks(repo, v.commit, specs, env, v.base.prep, prep)
    if interrupted is not None:
        raise interrupted
    integ = replace(v.integrity, tests_before=None, tests_after=None, fewer_tests_in=[])
    unavailable = prep.error is not None or not again or any(r.status == "not_run" for r in again)
    if not unavailable and integ.guard_files and v.commit != v.base.merge_base:
        if stopped := count_tests(repo, v.base, again, integ, env):
            raise stopped
    sig = v.signature()
    v2 = Verification(v.piece, v.commit, v.base, integ, again, int((time.monotonic() - t0) * 1000), None,
                      declared=list(v.declared), paths=v.paths, files=v.files, prepared=prep,
                      retry_of=first.experiment_id, first_signature=sig)
    no_decision = Outcome(None, "recorded without a decision (--no-decide)")
    if unavailable:
        v2.environment = prep.error or "retry did not run every declared check"
        v2.retry = RETRY_UNAVAILABLE
        _record(lg, v2, reported_by, report, trace_ref, found, receipt_id=receipt_id, discard=False)
        _discard(lg, v, first)
        first.seen_before = seen or []
        first.outcome = _decide(lg, v, first) if decide else no_decision
        return v, first
    v2.retry = (RETRY_PASSED if v2.all_passed else RETRY_FAILED if v2.signature() == sig else RETRY_FAILED_OTHER)
    rec = _record(lg, v2, reported_by, report, trace_ref, found, receipt_id=receipt_id,
                  discard=v2.retry == RETRY_FAILED)
    rec.seen_before = seen or []
    rec.outcome = _decide(lg, v2, rec) if decide else no_decision
    return v2, rec


_STORED = ("SELECT author, config, metrics, run_status, code_ref, duration_ms FROM benchbook.experiments"
           " WHERE id = %s AND project_id = %s")


def _stored_attempt(lg: Ledger, receipt_id: UUID, piece: str, commit: str) -> dict[str, Any] | None:
    """The attempt a request's receipt already recorded, None when there is none. One of another agent, piece or
    commit is refused, as record_experiment refuses it."""
    from ..errors import PermissionDenied

    rows = lg.sql_internal(_STORED, (receipt_id, lg.project_id))
    if not rows:
        return None
    row = rows[0]
    if row["author"] != lg.agent:
        raise PermissionDenied("the experiment receipt belongs to another agent")
    if (row["config"] or {}).get("piece") != piece or row["code_ref"] != commit:
        raise BenchbookError(f"the experiment receipt {receipt_id} records another piece or commit")
    return row


def _linked_discard(lg: Ledger, attempt: UUID) -> bool:
    return bool(lg.sql_internal(
        "SELECT 1 FROM benchbook.links l JOIN benchbook.claims c ON c.project_id = l.project_id AND c.id = l.dst_id"
        " WHERE l.project_id = %s AND l.src_id = %s AND l.src_type = 'experiment' AND l.dst_type = 'claim'"
        " AND l.relation = 'supports' AND 'discarded' = ANY(c.tags) LIMIT 1", (lg.project_id, attempt)))


def _from_stored(row: dict[str, Any], piece: str, commit: str, b: Base, scope: list[str]) -> Verification:
    """The Verification an attempt in the ledger records: statuses, declared checks, guard columns, signature."""
    m = row["metrics"] if isinstance(row["metrics"], dict) else {}
    statuses = m.get("checks")
    results = [CheckResult(str(n), s if s in STATUSES else "fail")
               for n, s in (statuses.items() if isinstance(statuses, dict) else ())]
    integ = Integrity(list(m.get("guard_files") or []), m.get("checks_changed") is True,
                      guard_weakened_files=list(m.get("guard_weakened_files") or []),
                      tests_before=m.get("tests_before"), tests_after=m.get("tests_after"),
                      fewer_tests_in=list(m.get("fewer_tests_checks") or []))
    if m.get("guard_weakened") is True and not integ.guard_weakened:
        integ.fewer_tests_in = ["(recorded)"]  # an attempt that recorded only the flag still needs review
    environment = None
    if m.get("environment") is True or row["run_status"] != "ok":
        environment = "recorded as an environment failure"
    approach = (row["config"] or {}).get("approach")
    return Verification(piece, commit, b, integ, results, int(row["duration_ms"] or 0), None,
                        approach=str(approach) if approach else None,
                        declared=list(m.get("checks_declared") or []), environment=environment,
                        baseline_signature=m.get("baseline_signature"), paths=scope,
                        recorded_signature=m.get("failure_signature"),
                        retry=m.get("retry") if isinstance(m.get("retry"), str) else None,
                        retry_of=_uuid(m.get("retry_of")),
                        first_signature=m.get("first_signature") if isinstance(m.get("first_signature"), str) else None)


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _signal_name(e: BaseException | None) -> str | None:
    if e is None:
        return None
    return signal.Signals(e.signum).name if isinstance(e, Interrupted) else "SIGINT"


def _record(lg: Ledger, v: Verification, reported_by: str | None, report: Report | None,
            trace_ref: dict[str, Any] | None = None, given: UUID | None = None, *,
            receipt_id: UUID | None = None, discard: bool = True) -> Recorded:
    """Step 4. The claim is the implementer's (proposed, never decided here); the attempt and the link are the
    verifier's, so the ledger shows two different agents. given: a claim already checked by given_claim."""
    claim: tuple[UUID, bool] | None = None
    if given is not None:
        claim = (given, True)
    elif reported_by is not None:
        claim = _completion_claim(lg, v, reported_by, report)
    r = lg.record_experiment(v.config(), v.metrics(), run_status=v.run_status, code_ref=v.commit, notes=v.notes(),
                             duration_ms=v.duration_ms, trace_ref=trace_ref, _receipt_id=receipt_id)
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
    if discard:
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
    from ..core.ids import short
    from ..core.ledger import reporter_of

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
    from ..core.dedup import statement_hash
    from ..core.ledger import reporter_of

    payload: dict[str, Any] = {"piece": v.piece, "commit": v.commit,
                               "reported_checks": report.mentions if report else []}
    if report is not None:
        payload["report"] = {"sha256": report.sha256, "bytes": report.bytes}
    if v.paths:  # T06: the brief finds the claim by the directories the commit changed
        payload["paths"] = list(v.paths)
    if v.files:  # T10: and compares these files with HEAD for "changed since"
        payload["files"] = list(v.files)
    proposer = lg
    if lg.token is None:
        proposer = lg.as_agent(reporter)
    else:  # a token is one agent: the verifier records the claim and names the reporter (ledger.implementers)
        payload["reported_by"] = reporter
    named = payload.get("reported_by")
    usual, with_reporter = statements(v.piece, v.commit, reporter)
    tags = [f"piece:{v.piece}", f"commit:{v.commit}", *(failures.FILE_TAG + p for p in v.paths)]

    def existing() -> UUID | None:
        for row in lg.sql_internal(_OWN_CLAIMS, (lg.project_id, tags[:2], v.piece, v.commit)):
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
    sig = v.signature()
    if v.run_status != "ok" or sig is None or getattr(v, "dirty", False):
        return
    try:
        c = failures.record_discarded(lg, rec.experiment_id, v.piece, sig, v.approach, v.commit, v.paths)
    except Exception as e:  # noqa: BLE001 - see the docstring
        rec.discarded_error = f"{type(e).__name__}: {str(e).strip()[:200]}"
        return
    rec.discarded_id, rec.discarded_new = c.id, c.duplicate_of is None


def _decide(lg: Ledger, v: Verification, rec: Recorded) -> Outcome:
    """Step 5. The ledger's rule (decide, rule all_pass) is what makes a completion verified; this only picks the
    decision the evidence calls for and asks the rule before writing anything."""
    from ..core.ids import short
    from ..core.ledger import DECIDERS, TRANSITIONS

    if rec.claim_id is None:
        return Outcome(None, "no completion claim to decide (no --reported-by)")
    if lg.role not in DECIDERS:
        return Outcome(None, f"recorded without a decision: {lg.agent} has role {lg.role}, and verify decides only "
                             "with lead authority (orchestrator or human role)")
    info = lg.completion_attempts(rec.claim_id)
    assert info is not None
    status, claim, attempts = info["status"], rec.claim_id, info["attempts"]
    human = f"benchbook decide {short('claim', claim)} verified|refuted --oracle human --as human --rationale ..."
    if touched := [a for a in attempts if a["guard_weakened"] is True or a["checks_changed"] is True]:
        files = sorted({f for a in touched for f in a["weakened_files"]})
        changed = any(a["checks_changed"] is True for a in touched)
        what = " and ".join(([f"{len(files)} guarded test file(s) in a way that can weaken them"] if files else [])
                            + (["[checks]"] if changed else []))
        return Outcome(None, f"no decision: the commit changed {what or 'its tests (fewer ran)'}, so a re-run cannot "
                             f"verify it; a person has to decide ({human})", [a["id"] for a in touched], person=True,
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
