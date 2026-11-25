"""What benchbook keeps of a piece's failed attempts (spec §8 P0, docs/code.md "Failure signatures and discarded
approaches").

- Seen before: when a check fails with a signature that an earlier attempt of the same piece already has, the run
  names that first attempt and the approaches tried. A match is a notice and never refuses anything. Two attempts
  fail the same way when their signatures share a `check:part` element (signature.elements), so a check run of one
  check matches a check verify run that failed that check among others.
- Discarded approaches: a finished, failed attempt of a commit leaves an observation claim tagged `discarded` and
  `piece:<piece>` (plus `approach:<name>` and the piece's `mod:` tags), one per (piece, signature, approach).
  A later failure with the same key adds its attempt as evidence instead of a second claim (as findings.py does for
  out-of-scope findings, with its own fixed hash domain). The claim stays when the piece is later verified: it
  records that this approach failed this way, which a verified commit does not undo.

The ledger is passed in, except for check run's lookup (quick_notice), which opens one plain connection after a
failure only: check run imports no database code on its fast path.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from . import signature

if TYPE_CHECKING:
    from .ledger import Ledger
    from .models import ClaimResult

DISCARDED_TAG = "discarded"
PIECE_TAG = "piece:"
APPROACH_TAG = "approach:"
MOD_TAG = "mod:"
FILE_TAG = "file:"  # a directory prefix the attempt's commit changed (T06)
CLAIM_KIND = "observation"
# Domain separator of the discarded-approach dedup hash, stored in claims.statement_hash: changing it would make the
# same failure after an upgrade a second claim.
DISCARDED_DOMAIN = b"benchbook:discarded\0"
APPROACH = re.compile(r"\S{1,100}")  # as a piece name: it becomes a tag
SHOWN = 50  # earlier attempts read per lookup
LOOKUP_TIMEOUT = 2.0  # seconds check run waits for the seen-before lookup; then it says nothing
STATEMENT_TIMEOUT_MS = 1500

# Earlier valid, finished attempts of a piece whose signature shares an element with this one, oldest first.
PRIOR_SQL = """
SELECT e.id, e.author, greatest(0, extract(epoch FROM now() - e.created_at))::bigint AS age_s,
       e.config ->> 'approach' AS approach, e.code_ref,
       coalesce(e.metrics -> 'worktree_dirty' = 'true'::jsonb, false) AS dirty
FROM benchbook.experiments_valid e
WHERE e.project_id = %(p)s AND e.config ->> 'piece' = %(piece)s AND e.run_status = 'ok'
  AND string_to_array(e.metrics ->> 'failure_signature', '; ') && %(el)s::text[]
ORDER BY e.created_at, e.id
LIMIT %(n)s
"""


@dataclass
class Prior:
    id: UUID
    author: str
    age_s: int
    approach: str | None
    commit: str | None
    dirty: bool


# ------------------------------------------------------------------------------------------- seen before

def earlier(lg: Ledger, piece: str, sig: str | None) -> list[Prior]:
    """The earlier attempts of piece that failed the same way, oldest first (at most SHOWN)."""
    el = signature.elements(sig)
    if not el:
        return []
    rows = lg.sql_internal(PRIOR_SQL, {"p": lg.project_id, "piece": piece, "el": el, "n": SHOWN})
    return [_prior(r) for r in rows]


def _prior(r: Any) -> Prior:
    g = r.get if isinstance(r, dict) else dict(zip(("id", "author", "age_s", "approach", "code_ref", "dirty"), r,
                                                   strict=True)).get
    return Prior(UUID(str(g("id"))), str(g("author")), int(g("age_s") or 0), g("approach"), g("code_ref"),
                 bool(g("dirty")))


def ago(seconds: int) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def notice(priors: Sequence[Prior], piece: str) -> str | None:
    """One line naming the first earlier attempt with this failure, its approach, and the approaches tried."""
    if not priors:
        return None
    first = priors[0]
    what = [first.author, ago(first.age_s)]
    what.append(f"approach {first.approach}" if first.approach else "no approach named")
    if first.dirty:
        what.append("uncommitted changes")
    line = f"seen before: piece {piece} failed the same way in e:{str(first.id)[:8]} ({', '.join(what)})"
    approaches = list(dict.fromkeys(p.approach for p in priors if p.approach))
    more = len(priors) - 1
    if more or len(approaches) > 1:
        n = f"{len(priors)}{'+' if len(priors) >= SHOWN else ''}"
        line += f"; {n} earlier attempt(s) with this failure"
        if approaches:
            line += f", approaches: {', '.join(approaches)}"
    return line + ". A notice: nothing was refused."


def quick_notice(project: str, piece: str, sig: str | None, timeout: float = LOOKUP_TIMEOUT) -> str | None:
    """check run's lookup, after a failure only: one plain connection, read-only, bounded by timeout and silent
    on any error (a database that is down, slow or unknown, an embedded server that is not running). It never
    starts the embedded server."""
    el = signature.elements(sig)
    if not el:
        return None
    import threading

    box: list[str | None] = []

    def work() -> None:
        try:
            box.append(notice(_quick_priors(project, piece, el, timeout), piece))
        except Exception:  # noqa: BLE001 - a notice is never worth an error on a check's output
            box.append(None)

    t = threading.Thread(target=work, name="benchbook-seen-before", daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else None


def _quick_priors(project: str, piece: str, el: list[str], timeout: float) -> list[Prior]:
    from .config import env

    url = env("DATABASE_URL")
    if not url:
        from .db.conn import data_dir

        if not (data_dir() / "postmaster.pid").exists():  # the embedded server is not running: do not start it
            return []
        from .db.conn import database_url

        url = database_url()
    import psycopg

    with psycopg.connect(url, connect_timeout=max(2, int(timeout)), autocommit=True) as conn:
        conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        rows = conn.execute(PRIOR_SQL, {"p": project, "piece": piece, "el": el, "n": SHOWN}).fetchall()
    return [_prior(r) for r in rows]


# ------------------------------------------------------------------------------------------- discarded approaches

def discarded_hash(piece: str, sig: str, approach: str | None) -> str:
    """Same piece, same signature, same approach (or none): same discarded approach, whoever ran it."""
    raw = DISCARDED_DOMAIN + "\0".join((piece, sig, approach or "")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def statement(piece: str, sig: str, approach: str | None) -> str:
    who = f"Approach {approach}" if approach else "An attempt without a named approach"
    s = f"{who} on piece {piece} failed: {sig}"
    return s if len(s) <= 500 else s[:499] + "…"


def module_tags(lg: Ledger, piece: str) -> list[str]:
    """The mod: tags of the piece's most recent task (benchbook piece start --tags mod:x), if it has one."""
    rows = lg.sql_internal("SELECT tags FROM benchbook.tasks WHERE project_id = %s AND %s = ANY(tags)"
                           " ORDER BY created_at DESC, id LIMIT 1", (lg.project_id, PIECE_TAG + piece))
    return [t for t in (rows[0]["tags"] if rows else []) if t.startswith(MOD_TAG)]


NOTE_URI = "benchbook-note:sha256:"  # the source row of a dead end or finding an agent wrote down itself


def note_source(lg: Ledger, text: str, title: str) -> UUID:
    """A note an agent wrote itself (benchbook discard, benchbook finding add) as a source row keyed by its digest, so
    the same note twice is one source. It is the evidence a noted record carries instead of a failed attempt or a
    report."""
    digest = hashlib.sha256(f"{lg.agent}\0{text}".encode()).hexdigest()
    rows = lg.sql_internal("SELECT id FROM benchbook.sources WHERE project_id = %s AND uri = %s"
                           " ORDER BY created_at LIMIT 1", (lg.project_id, NOTE_URI + digest))
    if rows:
        return UUID(str(rows[0]["id"]))
    return lg.add_source(NOTE_URI + digest, title=title[:300], sha256=digest)


def record_noted(lg: Ledger, piece: str, approach: str, why: str, commit: str | None = None) -> ClaimResult:
    """A dead end the lead (or any agent) knows without a failed check run: `benchbook discard`. The same claim as a
    failed attempt's, with the reason as its signature (`noted: <why>`) and the note as its evidence, so the brief
    lists it under discarded approaches. The boundary pilot's probes asked for dead ends that lived only in prompts."""
    sig = "noted: " + " ".join(why.split())[:300]
    sid = note_source(lg, f"discard\0{piece}\0{approach}\0{sig}", f"dead end noted by {lg.agent}")
    tags = [DISCARDED_TAG, PIECE_TAG + piece, APPROACH_TAG + approach, *module_tags(lg, piece)]
    payload: dict[str, Any] = {"piece": piece, "signature": sig, "approach": approach, "noted_by": lg.agent}
    if commit:
        payload["commit"] = commit
    return lg._propose_claim(CLAIM_KIND, statement(piece, sig, approach), payload=payload, tags=tags,
                             evidence=[(f"s:{sid}", "supports")], _dedup_hash=discarded_hash(piece, sig, approach))


def record_discarded(lg: Ledger, attempt_id: UUID, piece: str, sig: str, approach: str | None,
                     commit: str | None, paths: Sequence[str] = ()) -> ClaimResult:
    """The discarded-approach claim of a failed attempt, with the attempt linked supports. Idempotent per key.
    paths: the directory prefixes the commit changed (checks.path_prefixes), as payload.paths and file: tags, so the
    brief finds the claim by path without a piece start (T06)."""
    tags = [DISCARDED_TAG, PIECE_TAG + piece, *([APPROACH_TAG + approach] if approach else []),
            *module_tags(lg, piece), *(FILE_TAG + p for p in paths)]
    payload: dict[str, Any] = {"piece": piece, "signature": sig, "first_attempt": str(attempt_id)}
    if paths:
        payload["paths"] = list(paths)
    if approach:
        payload["approach"] = approach
    if commit:
        payload["commit"] = commit
    key = discarded_hash(piece, sig, approach)
    return lg._propose_claim(CLAIM_KIND, statement(piece, sig, approach), payload=payload, tags=tags,
                             evidence=[(f"e:{attempt_id}", "supports")], _dedup_hash=key)
