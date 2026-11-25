"""Pull-request evidence (docs/code.md, "CI evidence and the PR summary"): which pieces a range of commits
completes, by their `Benchbook-Piece:` trailer, what the ledger knows about each, and the one PR comment that
carries it. The GitHub Action (integrations/github-action) is a thin shell around these and `check verify`."""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .errors import BenchbookError, InvalidTransition
from .findings import FINDING_TAG

if TYPE_CHECKING:
    from .core.ledger import Ledger

TRAILER = "Benchbook-Piece"
MARKER = "<!-- benchbook-summary -->"  # first line of the comment; how a later run finds it
DISCARDED_TAG = "discarded"  # observation claims tagged discarded and piece:<piece> are its discarded approaches
VERIFIED, CLAIMED, DISAGREES, UNREPORTED = "verified", "claimed only", "re-run disagrees", "not reported"
STATES = (VERIFIED, CLAIMED, DISAGREES, UNREPORTED)
MAX_ROWS = 50  # a comment holds at most 65536 characters
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Piece:
    piece: str
    commit: str


@dataclass
class PieceState:
    """What the ledger says about one piece at the commit the pull request names for it."""
    piece: str
    commit: str
    state: str  # one of STATES
    why: str
    claim_id: UUID | None = None
    claim_status: str | None = None
    reporter: str | None = None  # ledger.reporter_of the claim: what check verify --reported-by takes
    implementers: list[str] = field(default_factory=list)
    passed: int = 0  # valid finished attempts of the piece at the commit in which every check passed
    failed: int = 0
    independent: list[str] = field(default_factory=list)  # authors of clean passing runs other than the implementer
    partial: int = 0  # passing runs of only some declared checks (--checks): never evidence of verified
    discarded: int = 0
    open_findings: list[UUID] = field(default_factory=list)
    guard_touched: bool | None = None  # None: no attempt of the commit says
    guard_files: list[str] = field(default_factory=list)  # the weakened ones (T04); every touched one for old runs
    guard_weakened: bool | None = None
    tests_added: list[str] = field(default_factory=list)  # guarded files only added to (T04)
    checks_changed: bool | None = None


# ---------------------------------------------------------------------------------------------------- git

def pieces(repo: Path, base: str, head: str) -> tuple[list[Piece], list[str]]:
    """The pieces the commits in base..head complete, oldest first, and the trailer values skipped as unusable. A
    piece's commit is the last commit of the range carrying its trailer (`git log --topo-order` lists no commit
    before its descendants, so the first one met is the last)."""
    from .checks import PIECE, resolve_commit, toplevel

    root = toplevel(repo)
    b, h = resolve_commit(root, base), resolve_commit(root, head)
    fmt = f"%H%x00%(trailers:key={TRAILER},valueonly,separator=%x1f)%x1e"
    p = subprocess.run(["git", "-C", str(root), "log", "--topo-order", f"--format={fmt}", f"{b}..{h}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise BenchbookError(f"git log {b[:12]}..{h[:12]}: {p.stderr.strip()}")
    seen: dict[str, tuple[int, int, str]] = {}  # piece -> (commit index, newest first; trailer index; sha)
    skipped: list[str] = []
    for i, entry in enumerate(e.strip("\n") for e in p.stdout.split("\x1e")):
        if not entry:
            continue
        sha, _, values = entry.partition("\x00")
        for j, raw in enumerate(v.strip() for v in values.split("\x1f")):
            if not raw:
                continue
            if not PIECE.fullmatch(raw):
                skipped.append(raw[:200])
            elif raw not in seen:
                seen[raw] = (i, j, sha.strip())
    order = sorted(seen.items(), key=lambda kv: (-kv[1][0], kv[1][1]))
    return [Piece(name, sha) for name, (_, _, sha) in order], skipped


# ---------------------------------------------------------------------------------------------------- the ledger

# The completion claim of a piece at a commit: tagged with both, and its payload naming the same piece and commit (a
# claim whose payload names another commit is not about this one: the rule reads attempts by the payload). Several
# agents can have reported it; a verified claim is shown first, then the oldest.
_CLAIM = ("SELECT c.id, c.author, c.payload, a.role AS author_role, s.status, s.last_oracle"
          " FROM benchbook.claims c"
          " JOIN benchbook.v_claim_status s ON s.project_id = c.project_id AND s.id = c.id"
          " LEFT JOIN benchbook.agents a ON a.project_id = c.project_id AND a.id = c.author"
          " WHERE c.project_id = %s AND c.kind = 'completion' AND c.tags @> %s"
          " AND c.payload ->> 'piece' = %s AND c.payload ->> 'commit' = %s"
          " ORDER BY s.status = 'verified' DESC, c.created_at, c.id LIMIT 1")
# the attempts the all_pass rule reads (Ledger._attempts): valid, finished, of this piece at this commit, not dirty
_ATTEMPTS = ("SELECT id, author, metrics FROM benchbook.experiments_valid WHERE project_id = %s AND run_status = 'ok'"
             " AND config ->> 'piece' = %s AND code_ref = %s"
             " AND metrics -> 'worktree_dirty' IS DISTINCT FROM 'true'::jsonb ORDER BY created_at, id")
_DISCARDED = ("SELECT count(*) AS n FROM benchbook.claims WHERE project_id = %s AND kind = 'observation'"
              " AND tags @> %s")
_FINDINGS = ("SELECT id FROM benchbook.v_claim_status WHERE project_id = %s AND kind = 'observation' AND tags @> %s"
             " AND status IN ('proposed', 'inconclusive') ORDER BY created_at, id")


def completion(lg: Ledger, p: Piece) -> tuple[UUID, str] | None:
    """The completion claim of p at its commit and its reporter, if anyone reported it: the claim id is what
    `check verify --claim` takes, so the CI's re-run is evidence for the very claim this summary shows, whatever its
    statement. payload.reported_by counts only on a claim recorded by a lead, a person or a harness agent; a
    worker's claim is the worker's report, whomever it names (ledger.reporter_of)."""
    rows = lg.sql_internal(_CLAIM, _claim_params(lg, p))
    return (rows[0]["id"], _reporter(rows[0])) if rows else None


def reporter(lg: Ledger, p: Piece) -> str | None:
    """Who reported p's completion at its commit (completion), if anyone did."""
    found = completion(lg, p)
    return found[1] if found else None


def _claim_params(lg: Ledger, p: Piece) -> tuple[Any, ...]:
    return lg.project_id, [f"piece:{p.piece}", f"commit:{p.commit}"], p.piece, p.commit


def _reporter(row: dict[str, Any]) -> str:
    from .core.ledger import reporter_of

    return reporter_of(str(row["author"]), row["author_role"], (row["payload"] or {}).get("reported_by"))


def describe(lg: Ledger, found: Sequence[Piece]) -> list[PieceState]:
    return [_describe(lg, p) for p in found]


def _describe(lg: Ledger, p: Piece) -> PieceState:
    from .core.ledger import attempt_summary

    pid, tag = lg.project_id, f"piece:{p.piece}"
    rows = lg.sql_internal(_CLAIM, _claim_params(lg, p))
    attempts = [attempt_summary(r["id"], r["author"], r["metrics"]) for r in
                lg.sql_internal(_ATTEMPTS, (pid, p.piece, p.commit))]
    s = PieceState(p.piece, p.commit, UNREPORTED, "")
    s.discarded = int(lg.sql_internal(_DISCARDED, (pid, [DISCARDED_TAG, tag]))[0]["n"])
    s.open_findings = [r["id"] for r in lg.sql_internal(_FINDINGS, (pid, [FINDING_TAG, tag]))]
    s.passed = sum(1 for a in attempts if a["passed"])
    s.failed = len(attempts) - s.passed
    s.guard_touched = _flag(a["guard_touched"] for a in attempts)
    s.checks_changed = _flag(a["checks_changed"] for a in attempts)
    s.guard_weakened = _flag(a["guard_weakened"] for a in attempts)
    s.guard_files = sorted({f for a in attempts if a["guard_weakened"] is True for f in a["weakened_files"]})
    s.tests_added = sorted({f for a in attempts if a["guard_touched"] is True for f in a["guard_files"]}
                           - set(s.guard_files))
    if not rows:
        s.why = "no completion claim for this piece at this commit" + _runs(s)
        return s
    claim = rows[0]
    info = lg.completion_attempts(claim["id"])  # the ledger's set: every completion of this piece and commit adds to it
    impl = set(info["implementers"]) if info else set()
    s.claim_id, s.claim_status, s.reporter = claim["id"], claim["status"], _reporter(claim)
    s.implementers = sorted(impl)
    others = [a for a in attempts if a["passed"] and a["author"] not in impl and a["via"] is None
              and a["guard_weakened"] is False and a["checks_changed"] is False]
    clean = [a for a in others if a["complete"]]
    s.partial = len(others) - len(clean)
    s.independent = sorted({a["author"] for a in clean})
    if s.claim_status == "verified":
        s.state, s.why = VERIFIED, f"decided verified (oracle {claim['last_oracle']})"
    elif s.claim_status in ("refuted", "inconclusive"):
        s.state, s.why = DISAGREES, f"the claim was decided {s.claim_status}"
    elif failed := [a for a in attempts if not a["passed"]]:
        who = ", ".join(sorted({a["author"] for a in failed}))
        s.state, s.why = DISAGREES, f"{len(failed)} run(s) of this commit failed (by {who})"
    elif s.guard_weakened or s.checks_changed:
        s.state = CLAIMED
        s.why = ("the commit weakened tests (deleted or changed test lines, skip markers, fewer tests, conftest) or "
                 "changed [checks]: a re-run cannot verify it, a person decides")
    elif not clean:
        s.state = CLAIMED
        s.why = "no independent re-run of every declared check yet (the implementer's own runs do not count"
        s.why += f"; {s.partial} run(s) of only some checks do not either)" if s.partial else ")"
    else:
        try:  # the ledger's own rule, so "verified" here means what decide would accept
            with lg._conn() as conn:
                lg._check_rerun(conn, claim["id"], {a["id"] for a in clean})
        except InvalidTransition as e:
            s.state, s.why = CLAIMED, str(e)[:300]
        else:
            s.state = VERIFIED
            s.why = f"an independent re-run passed; the claim is {s.claim_status}, nobody has decided it yet"
    return s


def _flag(values: Any) -> bool | None:
    """True when any attempt says true, False when at least one says false and none true, None when none says."""
    seen = [v for v in values if isinstance(v, bool)]
    return True if any(seen) else (False if seen else None)


def _runs(s: PieceState) -> str:
    return f"; {s.passed} run(s) passed, {s.failed} failed" if s.passed or s.failed else ""


# ---------------------------------------------------------------------------------------------------- markdown

def _code(text: str, limit: int = 60) -> str:
    """Inline code that cannot leave the table cell or the span: names come from commit trailers and agent names."""
    t = " ".join(str(text).split()).replace("`", "'")
    t = t if len(t) <= limit else t[:limit - 3] + "..."
    return "`" + t.replace("|", "\\|") + "`"


def _short(kind: str, id: UUID) -> str:
    from .core.ids import short

    return short(kind, id)


def render(states: Sequence[PieceState], *, project: str, agent: str, token: bool, run_url: str | None = None,
           skipped: Sequence[str] = ()) -> str:
    """The comment: MARKER first, one row per piece, the reasons, and what the states mean."""
    lines = [MARKER, "### benchbook: completions in this pull request", ""]
    if not states:
        lines.append(f"No commit of this pull request carries a `{TRAILER}:` trailer.")
    else:
        lines += ["| piece | commit | state | runs of this commit | discarded approaches | open out-of-scope findings "
                  "| tests changed |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for s in states[:MAX_ROWS]:
            runs = f"{s.passed} passed, {s.failed} failed"
            if s.independent:
                runs += f"; independent: {', '.join(_code(a, 40) for a in s.independent[:3])}"
            lines.append(f"| {_code(s.piece)} | `{s.commit[:12]}` | **{s.state}** | {runs} | {s.discarded} | "
                         f"{len(s.open_findings)} | {_tests(s)} |")
        if len(states) > MAX_ROWS:
            lines.append(f"\n... and {len(states) - MAX_ROWS} more piece(s); `benchbook pr summary` lists them all.")
        lines.append("")
        for s in states[:MAX_ROWS]:
            claim = f" (claim {_short('claim', s.claim_id)})" if s.claim_id else ""
            lines.append(f"- {_code(s.piece)}{claim}: {_escape(s.why)}")
            if s.open_findings:
                ids = ", ".join(_short("claim", i) for i in s.open_findings[:10])
                lines.append(f"  - open out-of-scope findings: {ids}{' ...' if len(s.open_findings) > 10 else ''}")
            if s.guard_files:
                shown = ", ".join(_code(f, 80) for f in s.guard_files[:5])
                lines.append(f"  - tests weakened: {shown}{' ...' if len(s.guard_files) > 5 else ''}")
            if s.tests_added:
                shown = ", ".join(_code(f, 80) for f in s.tests_added[:5])
                lines.append(f"  - tests added: {shown}{' ...' if len(s.tests_added) > 5 else ''}")
    if skipped:
        lines += ["", f"Skipped {len(skipped)} `{TRAILER}:` value(s) that are not a piece name (1-100 characters, "
                      "no spaces)."]
    lines += ["", "<details><summary>What the states mean</summary>", "",
              "- **verified**: every declared check passed in a re-run of that commit by an agent other than the "
              "implementer, nothing guarded was touched, `[checks]` is unchanged and no run of the commit failed "
              "(the code pack's rule; a lead may not have decided the claim yet), or a lead or a person decided it "
              "verified. It does not mean the code is correct or that the checks test the piece.",
              "- **claimed only**: a completion was reported and nothing has confirmed it: no independent re-run "
              "yet, or the commit changed tests, which a person decides.",
              "- **re-run disagrees**: a run of that commit failed, or the claim was decided refuted or inconclusive.",
              "- **not reported**: the commit carries the trailer, but the ledger has no completion claim for it.",
              "", "</details>", ""]
    who = (f"as {_code(agent, 40)} (per-agent token: the ledger binds these records to that agent)" if token else
           f"as {_code(agent, 40)} (owner connection: agent names are self-declared, not authenticated)")
    run = f" in [this workflow run]({run_url})" if run_url and _safe_url(run_url) else ""
    lines.append(f"Ledger project {_code(project, 40)}; this summary was written {who}{run}.")
    return "\n".join(lines) + "\n"


def _tests(s: PieceState) -> str:
    parts = []
    if s.guard_weakened:
        parts.append(f"weakened: {len(s.guard_files)} file(s)" if s.guard_files else "weakened: fewer tests ran")
    if s.checks_changed:
        parts.append("`[checks]` changed")
    if s.tests_added:
        parts.append(f"added: {len(s.tests_added)} file(s)")
    if parts:
        return ", ".join(parts)
    if s.guard_touched is None and s.checks_changed is None:
        return "unknown (no run)"
    return "no"


def _escape(text: str) -> str:
    """Plain text for markdown: our own reasons, with agent names in them. Nothing in them may render as HTML, a
    link or a mention."""
    t = " ".join(text.split())
    for ch in "\\`*_[]<>|#!":
        t = t.replace(ch, "\\" + ch)
    return t.replace("@", "@\N{ZERO WIDTH SPACE}")


def _safe_url(url: str) -> bool:
    return re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/%-]*", url) is not None


# ---------------------------------------------------------------------------------------------------- the comment

def upsert_comment(repo: str, pr: int, body: str, *, author: str | None = None, update_only: bool = False,
                   gh: str = "gh") -> tuple[str, int | None]:
    """Create or update the one comment that starts with MARKER on pull request pr, through `gh api` (GH_TOKEN from
    the environment). Only comments by `author`, when given, count as ours, so a comment anyone else writes with the
    marker is never taken for the summary. Extra copies (two runs that both created one) are deleted. Returns
    ("created" | "updated" | "skipped", comment id)."""
    if not REPO.fullmatch(repo):
        raise BenchbookError(f"--repo: OWNER/NAME, got {repo!r}")
    if pr < 1:
        raise BenchbookError("--pr: the pull request's number")
    if not body.startswith(MARKER):
        body = f"{MARKER}\n{body}"
    payload = json.dumps({"body": body})
    comments = _pages(_gh(gh, ["api", "--paginate", f"repos/{repo}/issues/{pr}/comments?per_page=100"]))
    ours = [c for c in comments if isinstance(c, dict) and str(c.get("body") or "").startswith(MARKER)
            and (author is None or (c.get("user") or {}).get("login") == author)]
    if ours:
        keep = int(ours[0]["id"])
        _gh(gh, ["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{keep}", "--input", "-"], payload)
        for extra in ours[1:]:
            _gh(gh, ["api", "-X", "DELETE", f"repos/{repo}/issues/comments/{int(extra['id'])}"])
        return "updated", keep
    if update_only:
        return "skipped", None
    made = json.loads(_gh(gh, ["api", "-X", "POST", f"repos/{repo}/issues/{pr}/comments", "--input", "-"], payload))
    return "created", int(made["id"])


def _gh(gh: str, args: list[str], stdin: str | None = None) -> str:
    try:
        p = subprocess.run([gh, *args], input=stdin if stdin is not None else "", capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except FileNotFoundError as e:
        raise BenchbookError(f"{gh} not found: the comment is posted with the GitHub CLI") from e
    except subprocess.TimeoutExpired as e:
        raise BenchbookError(f"{gh} {' '.join(args[:3])}: no answer in 120 s") from e
    if p.returncode != 0:
        raise BenchbookError(f"{gh} {' '.join(args[:3])} failed (exit {p.returncode}): {p.stderr.strip()[-500:]}")
    return p.stdout


def _pages(text: str) -> list[Any]:
    """`gh api --paginate` prints one JSON array per page, back to back."""
    dec, out, i = json.JSONDecoder(), [], 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        page, i = dec.raw_decode(text, i)
        out += page if isinstance(page, list) else [page]
    return out
