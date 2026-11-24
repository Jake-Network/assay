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
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from . import config as localcfg
from . import failures, signature
from .config import CheckSpec, VerifyPrep, parse_checks, parse_guard, parse_verify
from .errors import BenchbookError, InvalidTransition
from .globs import matching
from .runner import Interrupted, _trap_signals, forget_stopped, run_command, stop_commands

if TYPE_CHECKING:
    from .ledger import Ledger

# Where a commit keeps its declarations; .assay/ is the folder's name before the rename.
CONFIG_PATHS = (".benchbook/config.toml", ".assay/config.toml")
DEFAULT_BASE = "origin/main"
# Used when the merge-base declares no [guard] paths: a guard that is silently empty would report "untouched".
DEFAULT_GUARD_PATHS = ("tests/**", "**/conftest.py", ".benchbook/config.toml")
MAX_GUARD_FILES = 100  # stored in metrics; guard_touched is computed from all of them
# T04: an added line with one of these makes a guarded file weakened even when nothing was deleted.
SKIP_MARKERS = ("pytest.mark.skip", "pytest.mark.xfail", "pytest.skip(", "unittest.skip", "it.skip(", "describe.skip(",
                "test.skip(", "xit(", "t.Skip(", "#[ignore]", "@Disabled", "@Ignore")
# Files whose any change can weaken every test (an autouse fixture, a collection hook): never "added only".
ALWAYS_WEAKENING = ("conftest.py",)
MAX_REPORT_BYTES = 10_000_000
MAX_FAILED_SHOWN = 50  # failing test ids kept on a result for the table; the signature is made from all of them
PIECE = re.compile(r"\S{1,100}")
STATUSES = ("ok", "fail", "timeout", "not_run")
# The ledger credentials a verifier may hold (integrations/github-action exports the first two), under both prefixes,
# and libpq's own password sources. check verify runs the commit's code without them: the code under test is the
# implementer's, and must not write to the ledger as the verifier.
# Checks pinned in the ledger (T01): a note claim tagged PIN_TAG and piece:<piece>, or piece:* for the project. The
# declarations a lead pins before the work starts stand in for committed ones when the merge-base has no config.
PIN_TAG = "checks-pin"
PROJECT_PIN = "*"
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
class Base:
    ref: str
    sha: str
    merge_base: str
    config_path: str
    checks: dict[str, CheckSpec]
    guard_paths: list[str]
    guard_declared: bool
    source: str = "merge-base"  # merge-base: committed config.toml; pin: checks pinned in the ledger (T01)
    pin_id: UUID | None = None
    checks_sha256: str | None = None  # the pin's hash of its checks, guard paths and verify table
    prep: VerifyPrep = field(default_factory=VerifyPrep)  # [verify]: what the worktree needs first (T02)


@dataclass
class Prepared:
    """What run_checks did to the worktree before the checks (T02): the names of the copied files, whether prepare
    ran, and why the preparation failed (an environment failure: no check runs)."""
    copied: list[str] = field(default_factory=list)
    prepare: bool = False
    error: str | None = None

    def metrics(self) -> dict[str, Any] | None:
        return {"copied": list(self.copied), "prepare": self.prepare} if self.copied or self.prepare else None


@dataclass
class Integrity:
    guard_files: list[str]  # changed files (deleted ones included) that match a guard glob
    checks_changed: bool
    # T04: guard_files split by what the change did. added: new files; appended: lines added, none deleted or
    # changed; weakened: anything else, a skip marker added, or any change to a conftest.py.
    guard_added: list[str] = field(default_factory=list)
    guard_appended: list[str] = field(default_factory=list)
    guard_weakened_files: list[str] = field(default_factory=list)
    tests_before: int | None = None  # JUnit testcases that ran at the merge-base, over the JUnit checks
    tests_after: int | None = None
    # The JUnit checks that ran fewer testcases than at the merge-base, each compared with itself: tests added to one
    # check never make up for tests gone from another.
    fewer_tests_in: list[str] = field(default_factory=list)

    @property
    def guard_touched(self) -> bool:
        return bool(self.guard_files)

    @property
    def fewer_tests(self) -> bool:
        return bool(self.fewer_tests_in)

    @property
    def guard_weakened(self) -> bool:
        """What sends a commit to a person (T04): a guarded file weakened, or a JUnit check ran fewer tests than at
        the merge-base. Tests only added are not."""
        return bool(self.guard_weakened_files) or self.fewer_tests


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
    # The failure signature of an attempt already in the ledger (a retried request finishing from the stored run):
    # its results keep only the statuses, so the signature is the recorded one.
    recorded_signature: str | None = None

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
            from .ids import short

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


MAX_SCOPE_PATHS = 20


def _parent(prefix: str) -> str:
    """"a/b/c/" -> "a/b/", "a/" or "x.py" -> "" (the whole repository)."""
    head = prefix.rstrip("/").rpartition("/")[0]
    return head + "/" if head else ""


def path_prefixes(paths: Sequence[str], limit: int = MAX_SCOPE_PATHS) -> list[str]:
    """Changed files as the directory prefixes the brief scopes by (T06): each file's directory ("media/x.py" ->
    "media/"; a file at the root stays itself), prefixes inside another dropped, and the deepest lifted to their
    parent until at most limit remain. [""] is the whole repository."""
    def reduce(ps: set[str]) -> list[str]:
        if "" in ps:
            return [""]
        out: list[str] = []
        for p in sorted(ps):
            if not any(p.startswith(q) for q in out if q.endswith("/")):
                out.append(p)
        return out

    found = reduce({p.rsplit("/", 1)[0] + "/" if "/" in p else p for p in paths if p})
    while len(found) > limit:
        depth = max(p.rstrip("/").count("/") for p in found)
        found = reduce({_parent(p) if p.rstrip("/").count("/") == depth else p for p in found})
    return found


def changed_paths(repo: Path, merge_base: str, commit: str) -> list[str]:
    """Files that differ between the merge-base and the commit. --no-renames: a test moved out of a guarded folder
    must show up as the deletion it is, not only under its new name."""
    out = _git(repo, "diff", "--name-only", "--no-renames", "-z", merge_base, commit).stdout
    return [p for p in out.split("\0") if p]


@dataclass
class Pin:
    """Checks pinned in the ledger: a note claim (PIN_TAG) with the declarations in its payload."""
    id: UUID
    author: str
    piece: str  # PROJECT_PIN for the project's pin
    checks: dict[str, CheckSpec]
    guard_paths: list[str]
    verify: dict[str, Any]
    sha256: str
    source: str  # config: pinned from the working tree's config.toml; cli: from --check
    created_at: Any = None


def pin_hash(checks: dict[str, CheckSpec], guard_paths: Sequence[str], verify: dict[str, Any] | None) -> str:
    """The hash a pin keeps: its checks, guard paths and verify table serialised with sorted keys."""
    body = {"checks": {n: {"cmd": s.cmd, "junit": s.junit, "timeout": s.timeout} for n, s in checks.items()},
            "guard_paths": list(guard_paths), "verify": verify or {}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_check_options(items: Sequence[str]) -> dict[str, CheckSpec]:
    """--check NAME=CMD (repeatable) as CheckSpecs. A command with {junit} reads its JUnit report."""
    table: dict[str, Any] = {}
    for item in items:
        name, sep, cmd = item.partition("=")
        if not sep or not name.strip() or not cmd.strip():
            raise BenchbookError(f"--check {item!r}: expected NAME=CMD")
        if name.strip() in table:
            raise BenchbookError(f"--check: {name.strip()} given twice")
        table[name.strip()] = {"cmd": cmd.strip(), "junit": "{junit}" in cmd}
    return parse_checks(table, "--check")


def pin_checks(lg: Ledger, piece: str | None, specs: dict[str, CheckSpec], guard_paths: Sequence[str],
               verify: dict[str, Any] | None = None, *, source: str = "cli") -> tuple[Pin, bool]:
    """Pin the checks of PIECE (None: the project's) in lg's name. Returns the pin in effect and whether it is new:
    pinning what the latest pin of that scope already holds records nothing."""
    from .dedup import statement_hash

    if not specs:
        raise BenchbookError("nothing to pin: no [checks.<name>] in config.toml and no --check NAME=CMD")
    scope = PROJECT_PIN if piece is None else piece
    if scope != PROJECT_PIN and not PIECE.fullmatch(scope):
        raise BenchbookError("piece: 1-100 characters without spaces")
    sha = pin_hash(specs, guard_paths, verify)
    latest = _pins(lg, scope)
    if latest and latest[0].sha256 == sha and latest[0].author == lg.agent:
        return latest[0], False
    payload = {"piece": scope, "sha256": sha, "pin_source": source, "guard_paths": list(guard_paths),
               "verify": verify or {},
               "checks": {n: {"cmd": s.cmd, "junit": s.junit, "timeout": s.timeout} for n, s in specs.items()}}
    what = "the project" if scope == PROJECT_PIN else f"piece {scope}"
    statement = f"checks pinned for {what}: {', '.join(specs)} ({sha[:12]})"
    # Every pin is its own claim: a later pin of the same text must still be the latest one.
    c = lg._propose_claim("note", statement, payload, [PIN_TAG, f"piece:{scope}"],
                          _dedup_hash=statement_hash(f"{statement}\0{uuid4().hex}"))
    return _pins(lg, scope, only=c.id)[0], True


_PINS = ("SELECT id, author, payload, created_at FROM benchbook.claims WHERE project_id = %s AND kind = 'note'"
         " AND tags @> %s AND payload ->> 'piece' = %s ORDER BY created_at DESC, id DESC")


def _pins(lg: Ledger, scope: str, *, only: UUID | None = None) -> list[Pin]:
    """The pins of a scope, newest first (unreadable ones skipped)."""
    out = []
    for r in lg.sql_internal(_PINS, (lg.project_id, [PIN_TAG, f"piece:{scope}"], scope)):
        if only is not None and r["id"] != only:
            continue
        p = r["payload"] if isinstance(r["payload"], dict) else {}
        try:
            specs = parse_checks(p.get("checks"), f"pin {r['id']}")
            guard, _ = parse_guard({"paths": p.get("guard_paths") or []}, f"pin {r['id']}")
            raw = p.get("verify")
            verify: dict[str, Any] = raw if isinstance(raw, dict) else {}
            parse_verify(verify, f"pin {r['id']}")
        except BenchbookError:
            continue
        out.append(Pin(r["id"], str(r["author"]), scope, specs, guard, verify, str(p.get("sha256") or ""),
                       str(p.get("pin_source") or "cli"), r["created_at"]))
    return out


def pinned(lg: Ledger, piece: str, exclude: set[str] | frozenset[str] = frozenset()) -> Pin | None:
    """The pin check verify uses for PIECE when the merge-base has no config: the newest pin of the piece, else the
    newest pin of the project, leaving out pins written by any agent in exclude (the implementers)."""
    for scope in (piece, PROJECT_PIN):
        for p in _pins(lg, scope):
            if p.author not in exclude:
                return p
    return None


_FIRST_ATTEMPT = ("SELECT min(created_at) AS t FROM benchbook.experiments_valid"
                  " WHERE project_id = %s AND config ->> 'piece' = %s")


def pin_changed(lg: Ledger, pin: Pin, piece: str, repo: Path, exclude: set[str] | frozenset[str]) -> bool:
    """Whether the pinned checks changed under the work (T01): the working tree's config.toml no longer matches a pin
    taken from it, or the piece's first attempt ran under another pin (or none) than the one in effect now."""
    if pin.source == "config":
        cfg = localcfg.load(repo)
        if pin_hash(cfg.checks, cfg.guard_paths, cfg.verify.table()) != pin.sha256:
            return True
    first = lg.sql_internal(_FIRST_ATTEMPT, (lg.project_id, piece))[0]["t"]
    if first is None or pin.created_at is None or pin.created_at <= first:
        return False
    before = [p for scope in (piece, PROJECT_PIN) for p in _pins(lg, scope)
              if p.author not in exclude and p.created_at is not None and p.created_at <= first]
    return not before or max(before, key=lambda p: p.created_at).sha256 != pin.sha256


def implementers_of(lg: Ledger, piece: str, commit: str, reported_by: str | None) -> set[str]:
    """Who reported PIECE done at COMMIT: reported_by and the reporters of its completion claims so far."""
    from .ledger import reporter_of

    out = {reported_by} if reported_by else set()
    for row in lg.sql_internal(_OWN_CLAIMS, (lg.project_id, [f"piece:{piece}", f"commit:{commit}"], piece, commit)):
        out.add(reporter_of(row["author"], row["author_role"], (row["payload"] or {}).get("reported_by")))
    return out


def read_base(repo: Path, ref: str, commit: str, pin: Callable[[], Pin | None] | None = None) -> Base:
    """The checks a commit is verified against: the config.toml committed at the merge-base of ref and commit, or,
    when it has none, the pin that pin() finds (T01)."""
    sha = resolve_commit(repo, ref)
    mb = _git(repo, "merge-base", sha, commit, check=False).stdout.strip()
    if not mb:
        raise BenchbookError(f"{commit[:12]} has no common ancestor with the base {ref}")
    try:
        found = config_at(repo, mb)
    except _Unreadable as e:
        raise BenchbookError(f"the base's declarations are unreadable: {e}") from e
    if found is None and pin is not None and (p := pin()) is not None:
        from .ids import short

        return Base(ref, sha, mb, f"pin {short('claim', p.id)}", p.checks, p.guard_paths or list(DEFAULT_GUARD_PATHS),
                    bool(p.guard_paths), "pin", p.id, p.sha256, parse_verify(p.verify, f"pin {short('claim', p.id)}"))
    if found is None:
        raise BenchbookError(f"no .benchbook/config.toml at the merge-base {mb[:12]} of {ref} and {commit[:12]}, and "
                             f"no checks pinned in the ledger: commit the [checks] declarations to {ref}, or pin them "
                             "(benchbook check pin [PIECE])")
    data, rel = found
    where = f"{rel} at {mb[:12]}"
    checks = parse_checks(data.get("checks"), where)
    if not checks:
        raise BenchbookError(f"{where} declares no [checks.<name>]: commit the declarations to {ref} first")
    paths, _ = parse_guard(data.get("guard"), where)
    return Base(ref, sha, mb, rel, checks, paths or list(DEFAULT_GUARD_PATHS), bool(paths),
                prep=parse_verify(data.get("verify"), where))


def integrity(repo: Path, base: Base, commit: str, pin_moved: bool = False) -> Integrity:
    """Step 2. pin_moved: the pinned checks changed under the work (pin_changed), which counts as a [checks] change."""
    guard_files = matching(changed_paths(repo, base.merge_base, commit), base.guard_paths)
    added, appended, weakened = classify_guard(repo, base.merge_base, commit, guard_files)
    try:
        found = config_at(repo, commit)
    except _Unreadable:
        return Integrity(guard_files, True, added, appended, weakened)
    before = config_at(repo, base.merge_base)
    old = before[0].get("checks") if before else None
    new = found[0].get("checks") if found else None
    # parsed tables: comments and layout are no change
    return Integrity(guard_files, old != new or pin_moved, added, appended, weakened)


def classify_guard(repo: Path, merge_base: str, commit: str,
                   files: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """T04: (added, appended, weakened) of the changed guarded files. A new file is added and a file with lines added
    and none deleted is appended, unless an added line carries a SKIP_MARKERS entry or the file is a conftest.py;
    everything else (deleted, rewritten, binary) is weakened."""
    if not files:
        return [], [], []
    out = _git(repo, "diff", "--numstat", "--no-renames", "-z", merge_base, commit, "--", *files).stdout
    deleted: dict[str, int | None] = {}
    for entry in out.split("\0"):
        parts = entry.split("\t")
        if len(parts) == 3 and parts[2]:
            deleted[parts[2]] = int(parts[1]) if parts[1].isdigit() else None  # "-": binary
    status = {}
    names = _git(repo, "diff", "--name-status", "--no-renames", "-z", merge_base, commit, "--", *files).stdout
    items = [x for x in names.split("\0") if x]
    for st, path in zip(items[::2], items[1::2], strict=False):
        status[path] = st[:1]
    added: list[str] = []
    appended: list[str] = []
    weakened: list[str] = []
    for f in files:
        st, dels = status.get(f, ""), deleted.get(f)
        if Path(f).name in ALWAYS_WEAKENING or st not in ("A", "M") or dels is None or (st == "M" and dels > 0):
            weakened.append(f)
        elif _adds_skip(repo, merge_base, commit, f):
            weakened.append(f)
        else:
            (added if st == "A" else appended).append(f)
    return added, appended, weakened


def _adds_skip(repo: Path, merge_base: str, commit: str, path: str) -> bool:
    diff = _git(repo, "diff", "-U0", "--no-renames", "--no-color", merge_base, commit, "--", path).stdout
    return any(line.startswith("+") and not line.startswith("+++") and any(m in line for m in SKIP_MARKERS)
               for line in diff.splitlines())


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


# ---------------------------------------------------------------------------------------------------- baseline (T03)

@dataclass
class BaseRun:
    """One check's result at the merge-base."""
    status: str
    signature: str | None
    failed: list[str]
    tests: int | None


def baseline(repo: Path, b: Base, specs: Sequence[CheckSpec],
             env: dict[str, str] | None = None) -> tuple[dict[str, BaseRun], BaseException | None]:
    """Run the reference checks now, using the same environment as the candidate."""
    prepared = Prepared()
    results, interrupted = run_checks(repo, b.merge_base, specs, env, b.prep, prepared)
    if interrupted is not None or prepared.error is not None:
        return {}, interrupted
    return {r.name: BaseRun(r.status, r.signature, [t for t, _ in r.failed_tests], r.tests)
            for r in results}, None


class EarlyBaseline:
    """[verify] parallel_baseline: count_tests's merge-base run, started in a thread beside the commit's checks. Its
    result is used only when count_tests would have run exactly these checks; otherwise it runs them as before."""

    def __init__(self, repo: Path, b: Base, specs: list[CheckSpec], env: dict[str, str] | None) -> None:
        self.names = [s.name for s in specs]
        self._box: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._run, args=(repo, b, specs, env), daemon=True,
                                        name="benchbook-merge-base")
        self._thread.start()

    def _run(self, repo: Path, b: Base, specs: list[CheckSpec], env: dict[str, str] | None) -> None:
        try:
            self._box["value"] = baseline(repo, b, specs, env)
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            self._box["error"] = e

    def result(self) -> tuple[dict[str, BaseRun], BaseException | None]:
        """The run's result; an interruption while waiting for it (verify traps SIGTERM and SIGHUP while the run
        lives) stops the run and comes back as the run's interruption, as a sequential baseline's would."""
        try:
            while self._thread.is_alive():
                self._thread.join(0.1)
        except (Interrupted, KeyboardInterrupt) as e:
            with _signals_held():
                self.stop()
            return {}, e
        if "error" in self._box:
            raise self._box["error"]
        return self._box["value"]  # type: ignore[no-any-return]

    def stop(self) -> None:
        """End the run if it is still going (its check's process group is killed) and wait for its cleanup."""
        ident = self._thread.ident
        if ident is not None and self._thread.is_alive():
            stop_commands(ident)
            self._thread.join()
            forget_stopped(ident)


def count_tests(repo: Path, b: Base, results: list[CheckResult], integ: Integrity,
                env: dict[str, str] | None, early: EarlyBaseline | None = None) -> BaseException | None:
    """T04: the JUnit testcases that ran here and at the merge-base. Each JUnit check that reported a count is
    compared with its own merge-base run (integ.fewer_tests_in names those that ran fewer); tests_before and
    tests_after are the sums, for the record. Run only when guarded files changed; the merge-base runs come from
    the current environment. Returns an interruption of that run."""
    counted = [r for r in results if b.checks[r.name].junit and r.tests is not None]
    if not counted:
        return None
    if early is not None and early.names == [r.name for r in counted]:
        runs, interrupted = early.result()
    else:
        runs, interrupted = baseline(repo, b, [b.checks[r.name] for r in counted], env)
    if interrupted is not None:
        return interrupted
    before = after = 0
    seen = False
    fewer = []
    for r in counted:
        was = runs[r.name].tests if r.name in runs else None
        if isinstance(was, int) and r.tests is not None:
            before, after, seen = before + was, after + r.tests, True
            if r.tests < was:
                fewer.append(r.name)
    if seen:
        integ.tests_before, integ.tests_after, integ.fewer_tests_in = before, after, fewer
    return None


def same_failure(r: CheckResult, base: BaseRun) -> bool:
    """Whether a failing check fails the same way at the merge-base: an equal signature element, or (JUnit) every
    test failing here fails there too."""
    if base.status not in ("fail", "timeout"):
        return False
    if r.signature and base.signature and r.signature == base.signature:
        return True
    mine = {t for t, _ in r.failed_tests}
    return bool(mine) and len(r.failed_tests) < MAX_FAILED_SHOWN and mine <= set(base.failed)


# The checks an implementer's own clean run of PIECE at COMMIT passed (check run, the Bash hook: metrics.via): run
# by one of the piece's implementers (the attempt's author, or metrics.run_by, the agent type the Bash hook saw) and
# of the definition verify runs (metrics.check_definitions, CheckSpec.definition). A run without either says nothing
# about this check. A raw Bash pass of a JUnit check proves nothing (docs/claude-code.md) and is left out, as the
# completion gate does.
_PASSED_ELSEWHERE = (
    "SELECT DISTINCT c.key FROM benchbook.experiments_valid e,"
    " jsonb_each_text(CASE WHEN jsonb_typeof(e.metrics -> 'checks') = 'object' THEN e.metrics -> 'checks'"
    " ELSE '{}'::jsonb END) AS c(key, value)"
    " WHERE e.project_id = %s AND e.config ->> 'piece' = %s AND e.code_ref = %s AND e.run_status = 'ok'"
    " AND e.metrics ->> 'via' IS NOT NULL AND e.metrics -> 'worktree_dirty' IS DISTINCT FROM 'true'::jsonb"
    " AND c.value = 'ok' AND (c.key <> ALL(%s::text[]) OR e.metrics ->> 'via' IS DISTINCT FROM 'bash-hook')"
    " AND (e.author = ANY(%s::text[]) OR e.metrics ->> 'run_by' = ANY(%s::text[]))"
    " AND jsonb_typeof(e.metrics -> 'check_definitions') = 'object'"
    " AND e.metrics -> 'check_definitions' ->> c.key = %s::jsonb ->> c.key")


def passed_elsewhere(lg: Ledger, piece: str, commit: str, b: Base, implementers: Collection[str]) -> set[str]:
    """The checks of b that an implementer's own clean run of piece at commit passed, under b's definition."""
    if not implementers:
        return set()
    junit = [n for n, s in b.checks.items() if s.junit]
    who = sorted(implementers)
    defs = json.dumps({n: s.definition for n, s in b.checks.items()})
    return {str(r["key"]) for r in lg.sql_internal(_PASSED_ELSEWHERE, (lg.project_id, piece, commit, junit, who, who,
                                                                       defs))}


def classify_environment(repo: Path, b: Base, results: list[CheckResult], env: dict[str, str] | None,
                         elsewhere: set[str]) -> tuple[str | None, str | None, BaseException | None]:
    """T03: a failing check is an environment failure when the implementer's own clean run of this commit passed it
    (elsewhere) and it fails the same way at the merge-base, which is then re-run. A check that fails at the
    merge-base by design (the piece's goal) and was never seen passing stays the piece's failure. Returns (the
    reason when every failing check is an environment failure, the merge-base's signature of those checks, an
    interruption of the baseline run)."""
    failing = [r for r in results if r.status in ("fail", "timeout")]
    if not failing or not all(r.name in elsewhere for r in failing):
        return None, None, None
    runs, interrupted = baseline(repo, b, [b.checks[r.name] for r in failing], env)
    for r in failing:
        r.environment = r.name in runs and same_failure(r, runs[r.name])
    if interrupted is not None or not all(r.environment for r in failing):
        return None, None, interrupted
    names = ", ".join(r.name for r in failing)
    sig = signature.combine((r.name, runs[r.name].signature) for r in failing)
    return (f"{names} {'fails' if len(failing) == 1 else 'fail'} the same way at the merge-base "
            f"{b.merge_base[:12]}", sig, None)


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
           receipt_id: UUID | None = None) -> tuple[Verification, Recorded]:
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
    scope = path_prefixes(changed_paths(repo, b.merge_base, sha))
    found = None if given is None else given[0]
    if receipt_id is not None and (stored := _stored_attempt(lg, receipt_id, piece, sha)) is not None:
        # A retry of a request whose attempt is already in the ledger (the result cache was not written, a later
        # step failed): finish the links and the decision from that attempt, never from a new run, which could
        # disagree with what is stored.
        v = _from_stored(stored, piece, sha, b, scope)
        sig = v.signature()
        seen = [p for p in failures.earlier(lg, piece, sig) if p.id != receipt_id] if sig else []
        rec = _record(lg, v, reported_by, report, trace_ref, found, receipt_id=receipt_id)
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
        results, interrupted = run_checks(repo, sha, [b.checks[n] for n in names], env, b.prep, prepared)
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
                     paths=scope)
    sig = failure_signature(results) if interrupted is None else None
    seen = failures.earlier(lg, piece, sig) if sig else []  # before recording: this run is not its own match
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


_STORED = ("SELECT author, config, metrics, run_status, code_ref, duration_ms FROM benchbook.experiments"
           " WHERE id = %s AND project_id = %s")


def _stored_attempt(lg: Ledger, receipt_id: UUID, piece: str, commit: str) -> dict[str, Any] | None:
    """The attempt a request's receipt already recorded, None when there is none. One of another agent, piece or
    commit is refused, as record_experiment refuses it."""
    from .errors import PermissionDenied

    rows = lg.sql_internal(_STORED, (receipt_id, lg.project_id))
    if not rows:
        return None
    row = rows[0]
    if row["author"] != lg.agent:
        raise PermissionDenied("the experiment receipt belongs to another agent")
    if (row["config"] or {}).get("piece") != piece or row["code_ref"] != commit:
        raise BenchbookError(f"the experiment receipt {receipt_id} records another piece or commit")
    return row


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
                        recorded_signature=m.get("failure_signature"))


def _signal_name(e: BaseException | None) -> str | None:
    if e is None:
        return None
    return signal.Signals(e.signum).name if isinstance(e, Interrupted) else "SIGINT"


def _record(lg: Ledger, v: Verification, reported_by: str | None, report: Report | None,
            trace_ref: dict[str, Any] | None = None, given: UUID | None = None, *,
            receipt_id: UUID | None = None) -> Recorded:
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
    if v.paths:  # T06: the brief finds the claim by the directories the commit changed
        payload["paths"] = list(v.paths)
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
