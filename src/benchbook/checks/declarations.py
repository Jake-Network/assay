"""Which checks a verification runs: the declarations of the merge-base, or the ones a lead pinned in the ledger before
the work started (T01), and whether a pin moved since the first attempt."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from .. import config as localcfg
from ..config import CheckSpec, VerifyPrep, parse_checks, parse_guard, parse_verify
from ..errors import BenchbookError
from .gitrepo import _git, _Unreadable, config_at, resolve_commit

if TYPE_CHECKING:
    from ..core.ledger import Ledger

# Used when the merge-base declares no [guard] paths: a guard that is silently empty would report "untouched".
DEFAULT_GUARD_PATHS = ("tests/**", "**/conftest.py", ".benchbook/config.toml")
PIECE = re.compile(r"\S{1,100}")
# The ledger credentials a verifier may hold (integrations/github-action exports the first two), under both prefixes,
# and libpq's own password sources. check verify runs the commit's code without them: the code under test is the
# implementer's, and must not write to the ledger as the verifier.
# Checks pinned in the ledger (T01): a note claim tagged PIN_TAG and piece:<piece>, or piece:* for the project. The
# declarations a lead pins before the work starts stand in for committed ones when the merge-base has no config.
PIN_TAG = "checks-pin"
PROJECT_PIN = "*"


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
    from ..core.dedup import statement_hash

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
        from ..core.ids import short

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
