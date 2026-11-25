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

import psycopg

from ..core import globs
from ..core.ids import short
from ..errors import BenchbookError, LeaseLost, ValidationFailed

if TYPE_CHECKING:
    from ..core.ledger import Ledger
    from ..core.models import Task

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
    declared = _declared(patterns)
    paths, new = expand(declared, tracked_files(repo)) if patterns else (None, [])
    with lg._intent_transaction() as conn:
        previous = _latest(conn, lg, piece)
        if previous is not None and previous["live"]:
            patch = {} if paths is None else {"paths": paths, "declared": declared}
            try:
                task = lg._update_intent(conn, previous["id"], patch, lease, ["piece"], ["paths"])
            except LeaseLost:
                # The database clock reached expiry before the renewal. Re-read under the same lock.
                previous = _latest(conn, lg, piece)
            else:
                extra = [tag for tag in tags if tag not in task.tags]
                note = (f"tags are fixed when a piece starts; {', '.join(extra)} not added (piece done, then start "
                        "again)" if extra else None)
                return Started(task, True, list(task.spec.get("paths", [])), new, note)
        if paths is None:
            declared = _recover_paths(previous)
            if not declared:
                raise BenchbookError(f"declare the paths piece {piece} will change: --paths 'glob' (repeatable)")
            assert previous is not None
            paths, new = expand(declared, tracked_files(repo))
            tags = [*_extra_tags(previous["tags"], piece), *tags]
        spec = {"piece": piece, "paths": paths, "declared": declared}
        task = lg._create_intent(conn, f"piece {piece}", spec, [PIECE_TAG, f"piece:{piece}", *tags], lease,
                                 ["piece"], ["paths"])
        return Started(task, False, paths, new)


def _extra_tags(tags: Sequence[str], piece: str) -> list[str]:
    return [x for x in tags if x not in ("intent", PIECE_TAG, f"piece:{piece}")]


# Creation events are ordered under the intent lock. A task's timestamp can predate the lock wait, or the wall
# clock can step backwards; neither should make a released generation look older than one it replaced.
_GENERATION_ORDER = ("ORDER BY (SELECT id FROM benchbook.task_events e WHERE e.task_id = t.id"
                     " AND e.event = 'created' ORDER BY e.id LIMIT 1) DESC NULLS LAST, t.created_at DESC, t.id DESC")


def _latest(conn: psycopg.Connection[Any], lg: Ledger, piece: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT t.id, t.status, t.spec -> 'declared' AS declared, t.tags,"
        " t.status = 'claimed' AND t.lease_expires_at > clock_timestamp() AS live,"
        " t.lease_expires_at IS NOT NULL AS has_expiry FROM benchbook.tasks t"
        " WHERE t.project_id=%s AND t.lease_owner=%s AND 'intent' = ANY(t.tags)"
        f" AND %s = ANY(t.tags) AND t.spec ->> 'piece' = %s {_GENERATION_ORDER} LIMIT 1",
        (lg.project_id, lg.agent, PIECE_TAG, piece),
    ).fetchone()
    return row


def _recover_paths(previous: dict[str, Any] | None) -> list[str]:
    # Automatic expiry retains the deadline, including failed intents closed by next_task. An explicit
    # completion or cancellation clears it. Only the latest generation is eligible for recovery.
    if (previous is None or previous["live"] or not previous["has_expiry"]
            or previous["status"] not in ("claimed", "failed") or not isinstance(previous["declared"], list)):
        return []
    return [path for path in previous["declared"] if isinstance(path, str)]


def done(lg: Ledger, piece: str, failed: bool = False, summary: str = "") -> Task:
    """Release PIECE. Its holder marks it done (or failed); a lead (orchestrator or human) may release another
    agent's piece, which cancels that intent. Raises when there is nothing to release."""
    from ..core.ledger import DECIDERS

    piece = check_name(piece)
    with lg._intent_transaction() as conn:
        rows = conn.execute(
            "SELECT t.id, t.lease_owner, t.lease_expires_at > clock_timestamp() AS live FROM benchbook.tasks t"
            " WHERE t.project_id=%s AND t.status='claimed' AND %s = ANY(t.tags) AND 'intent' = ANY(t.tags)"
            f" AND t.spec ->> 'piece' = %s {_GENERATION_ORDER}", (lg.project_id, PIECE_TAG, piece),
        ).fetchall()
        released = None
        for row in rows:
            if row["lease_owner"] == lg.agent:
                task = lg._complete_task(conn, row["id"], "failed" if failed else "done", summary)
                released = released or task
        if released is not None:
            return released
        live = next((row for row in rows if row["live"]), None)
        if live is not None:
            if lg.role in DECIDERS:
                return lg._cancel_task(conn, live["id"], f"piece {piece} released by {lg.agent}"
                                       + (f": {summary}" if summary else ""))
            tid = short("task", live["id"])
            raise BenchbookError(f"piece {piece} is held by {live['lease_owner']} ({tid}); only its holder or a lead "
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
