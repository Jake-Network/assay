"""Pieces of coding work and the paths they occupy (spec §8 "P1 path occupancy", docs/code.md).

A piece is a worker intent (Ledger.create_intent): a task tagged `intent`, `piece` and `piece:<name>`, held by
the agent that started it, with spec {"piece", "paths", "declared"}. `declared` is what --paths was given; `paths`
is that expanded against `git ls-files`: every tracked file a glob matches (escaped with globs.escape, so a file
name with [ or * in it stays one file), and the glob itself when it matches no tracked file yet (the piece will add
it) or more than MAX_FILES_PER_GLOB (the glob then stands for those files and any new ones under it). The ledger
refuses the intent when another live intent holds the same piece or paths that overlap (Ledger._check_paths, under
the advisory lock that already serialises the exclusive keys), so of two concurrent starts exactly one wins.

The occupancy lasts while the intent's lease does: `piece start` again by the same agent renews it (and replaces
its paths when --paths is given), `piece done` releases it, and an expired lease frees the paths without anyone
acting. Nothing here adds a table, a status or a queue (spec §6)."""
from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import globs
from .errors import BenchbookError, LeaseLost, ValidationFailed
from .ids import short

if TYPE_CHECKING:
    from .ledger import Ledger
    from .models import Task

PIECE_TAG = "piece"
# Long enough for one implementer session; a crashed one frees its paths after this. Renew with `piece start`.
PIECE_LEASE = 7200
# A glob matching more files than this is kept as the glob: the spec stays small enough to read in a brief, and
# the glob still occupies every file it matches.
MAX_FILES_PER_GLOB = 200
_NAME = re.compile(r"^[^\s\x00-\x1f]{1,100}$")


@dataclass(frozen=True)
class Started:
    task: Task
    renewed: bool  # the agent already held this piece: same task, lease renewed
    paths: list[str]  # what the piece occupies now
    new: list[str]  # declared globs that match no tracked file, kept as globs
    note: str | None = None


def check_name(piece: str) -> str:
    name = piece.strip()
    if not _NAME.match(name):
        raise ValidationFailed(["piece: 1 to 100 characters, no spaces or control characters"])
    return name


def tracked_files(repo: Path) -> list[str]:
    """Paths `git ls-files` lists from the repository root, '/'-separated."""
    try:
        top = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, check=True).stdout.strip()
        out = subprocess.run(["git", "-C", top, "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise BenchbookError(f"piece paths are expanded against git ls-files; {repo} is not in a git repository "
                             f"({getattr(e, 'stderr', '') or e})".strip()) from e
    return [p for p in out.split("\0") if p]


def _declared(patterns: Sequence[str]) -> list[str]:
    return [p.strip() for p in patterns if p.strip()]


def expand(patterns: Sequence[str], files: Sequence[str]) -> tuple[list[str], list[str]]:
    """(paths, new): the occupied path set and the declared globs that matched no tracked file."""
    declared = _declared(patterns)
    problems = globs.invalid_patterns(declared, max_length=500)
    if problems:
        raise ValidationFailed([f"--paths {p}" for p in problems])
    tracked = set(files)
    paths: list[str] = []
    new: list[str] = []
    for p in declared:
        if globs.normalise(p) in tracked:  # a tracked file named as it is, even with [ or * in its name
            paths.append(globs.escape(globs.normalise(p)))
            continue
        hits = globs.matching(files, [p])
        if not hits:
            new.append(p)
        if not hits or len(hits) > MAX_FILES_PER_GLOB:
            paths.append(globs.normalise(p))
        else:
            paths += [globs.escape(f) for f in hits]
    return sorted(dict.fromkeys(paths)), new


def start(lg: Ledger, piece: str, patterns: Sequence[str], repo: Path, lease_seconds: int | None = None,
          tags: Sequence[str] = ()) -> Started:
    """Start (or renew) PIECE as an intent of lg's agent occupying PATTERNS expanded in REPO. Raises Conflict when
    another live intent holds this piece or overlapping paths; nothing is written then."""
    piece = check_name(piece)
    lease = PIECE_LEASE if lease_seconds is None else lease_seconds
    mine = [t for t in lg.my_intents() if PIECE_TAG in t.tags and t.spec.get("piece") == piece]
    if not patterns and not mine and (last := _expired(lg, piece)) is not None:
        patterns, tags = last["declared"], [*last["tags"], *tags]  # start again what ran out, as it was declared
    if not patterns and not mine:
        raise BenchbookError(f"declare the paths piece {piece} will change: --paths 'glob' (repeatable)")
    paths, new = expand(patterns, tracked_files(repo)) if patterns else (None, [])
    if mine:
        cur = mine[0]
        patch: dict[str, Any] = {} if paths is None else {"paths": paths, "declared": _declared(patterns)}
        try:
            t = lg.update_intent(cur.id, patch, lease_seconds=lease, exclusive=["piece"], exclusive_paths=["paths"])
        except LeaseLost:
            # The lease ran out since my_intents read it: the paths are free for anyone, so they are taken again
            # as a new piece, through the overlap check, never by reviving the old one.
            if paths is None:
                patterns = [p for p in cur.spec.get("declared") or [] if isinstance(p, str)]
                paths, new = expand(patterns, tracked_files(repo))
            tags = [*_extra_tags(cur.tags, piece), *tags]
        else:
            extra = [x for x in tags if x not in cur.tags]
            note = (f"tags are fixed when a piece starts; {', '.join(extra)} not added (piece done, then start "
                    "again)" if extra else None)
            return Started(t, True, list(t.spec.get("paths", [])), new, note)
    assert paths is not None
    spec = {"piece": piece, "paths": paths, "declared": _declared(patterns)}
    t = lg.create_intent(f"piece {piece}", spec, tags=[PIECE_TAG, f"piece:{piece}", *tags], lease_seconds=lease,
                         exclusive=["piece"], exclusive_paths=["paths"])
    return Started(t, False, paths, new)


def _extra_tags(tags: Sequence[str], piece: str) -> list[str]:
    return [x for x in tags if x not in ("intent", PIECE_TAG, f"piece:{piece}")]


def _expired(lg: Ledger, piece: str) -> dict[str, Any] | None:
    """lg's newest intent for PIECE, when its lease ran out without a release: its declared globs and extra tags, so
    `piece start` without --paths takes the paths again (through the overlap check) instead of failing.

    Ran out: still claimed past its expiry, or failed by expiry. next_task fails an exhausted intent whose lease ran
    out and leaves lease_expires_at as it was; a release (piece done, done --failed, a lead's cancel) clears it. So a
    failed intent that still has an expiry ran out, and one without was ended on purpose: nothing to restore."""
    rows = lg.sql_internal(
        "SELECT spec -> 'declared' AS declared, tags,"
        " (status = 'claimed' AND lease_expires_at <= now()) OR (status = 'failed' AND lease_expires_at IS NOT NULL)"
        " AS ran_out FROM benchbook.tasks WHERE project_id=%s AND lease_owner=%s AND 'intent' = ANY(tags)"
        " AND %s = ANY(tags) AND spec ->> 'piece' = %s ORDER BY created_at DESC LIMIT 1",
        (lg.project_id, lg.agent, PIECE_TAG, piece))
    if not rows or not rows[0]["ran_out"] or not isinstance(rows[0]["declared"], list):
        return None
    declared = [p for p in rows[0]["declared"] if isinstance(p, str)]
    return {"declared": declared, "tags": _extra_tags(rows[0]["tags"] or [], piece)} if declared else None


def done(lg: Ledger, piece: str, failed: bool = False, summary: str = "") -> Task:
    """Release PIECE. Its holder marks it done (or failed); a lead (orchestrator or human) may release another
    agent's piece, which cancels that intent. Raises when there is nothing to release."""
    from .ledger import DECIDERS

    piece = check_name(piece)
    rows = lg.sql_internal(
        "SELECT id, lease_owner, lease_expires_at > now() AS live FROM benchbook.tasks WHERE project_id=%s"
        " AND status='claimed' AND %s = ANY(tags) AND 'intent' = ANY(tags) AND spec ->> 'piece' = %s"
        " ORDER BY created_at DESC", (lg.project_id, PIECE_TAG, piece))
    released: Task | None = None
    for r in rows:
        if r["lease_owner"] != lg.agent:
            continue
        try:
            t = lg.complete_task(r["id"], "failed" if failed else "done", summary)
        except LeaseLost:
            continue  # its lease ran out and another agent's claim marked it failed meanwhile
        released = released or t
    if released is not None:
        return released
    live = [r for r in rows if r["live"]]
    if live and lg.role in DECIDERS:
        return lg.cancel_task(live[0]["id"], f"piece {piece} released by {lg.agent}"
                              + (f": {summary}" if summary else ""))
    if live:
        tid = short("task", live[0]["id"])
        raise BenchbookError(f"piece {piece} is held by {live[0]['lease_owner']} ({tid}); only its holder or a lead "
                             f"(orchestrator or human) can release it")
    raise BenchbookError(f"no live piece {piece}: nothing to release")


def held(lg: Ledger, tags: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The live pieces, oldest first, through the reader view v_inflight (which a brief can read the same way)."""
    rows = lg.sql_internal(
        "SELECT id, agent, lease_left_s, spec, tags, created_at FROM benchbook.v_inflight WHERE project_id=%s"
        " AND is_intent AND %s = ANY(tags) AND tags @> %s::text[] ORDER BY created_at",
        (lg.project_id, PIECE_TAG, list(tags)))
    return [{"piece": r["spec"].get("piece"), "task": short("task", r["id"]), "agent": r["agent"],
             "lease_left_s": r["lease_left_s"], "declared": r["spec"].get("declared", []),
             "paths": r["spec"].get("paths", []),
             "tags": [x for x in r["tags"] if x not in ("intent", PIECE_TAG) and not x.startswith("piece:")]}
            for r in rows]
