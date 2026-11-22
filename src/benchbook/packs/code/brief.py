"""The code pack's brief (spec §8 P0): what an implementer needs before touching a module, within the token budget.

`benchbook brief --tags mod:media` (MCP benchbook_brief(tags=[...])) shows, for the given tags:

1. Discarded approaches: observation claims tagged `discarded` (failures.py) of the pieces in scope, with their
   signature, approach and first attempt, most recently failed first.
2. Open out-of-scope findings: observations tagged `out-of-scope` still `proposed` or `inconclusive` (findings.py),
   matched by tag or, for a `file:<prefix>` tag, by path prefix; most reported first.
3. Occupied paths: live piece intents (v_inflight) with their tags and spec.paths (pieces.py).
4. The last verified commit of each piece in scope, most recent first.

Scope: a tag matches records carrying it exactly (`mod:media`, `piece:x`); `file:src/app/media/` matches by prefix,
both the `file:` tags of findings and the paths pieces occupy. A piece is in scope when one of its tasks (benchbook
piece start) carries a matching tag or occupies a matching path; discarded approaches and verified commits are found
through their piece, and a discarded-approach claim also through its own mod: tags. Without tags the brief covers
the whole project.

Deterministic given (ledger state, tags, now). Sections are trimmed bottom-up, item by item, to the budget, and each
says how many items it left out; the header and the footer always stay.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ...brief import Section, age
from ...errors import ValidationFailed
from ...ids import short
from ...tokens import estimate_tokens

if TYPE_CHECKING:
    from ...ledger import Ledger

FOOTER = ("\nDetails: benchbook get <id> (one record, at most 400 tokens). Name your approach when you run a check "
          "(benchbook check run <check> --approach NAME, or BENCHBOOK_APPROACH): a failure seen before is named in the "
          "run's output.\n")
LIMITS = {"discarded": 8, "findings": 8, "occupied": 8, "verified": 8, "reported": 8}  # pack.toml [brief] overrides
GIT_TIMEOUT = 2.0  # seconds per git call of the validity check (T10)
_TEST_FILE = re.compile(r"([\w./-]+?)::")
FETCH = 200  # rows read per section; the section's total counts all of them
FILE_TAG = "file:"


@dataclass(frozen=True)
class Scope:
    exact: tuple[str, ...]  # tags matched as they are
    prefixes: tuple[str, ...]  # file:<prefix> tags, without "file:"

    @classmethod
    def of(cls, tags: Sequence[str] | None) -> Scope:
        tags = [t.strip() for t in tags or [] if t and t.strip()]
        return cls(tuple(dict.fromkeys(t for t in tags if not t.startswith(FILE_TAG))),
                   tuple(dict.fromkeys(t[len(FILE_TAG):] for t in tags if t.startswith(FILE_TAG))))

    @property
    def everything(self) -> bool:
        return not self.exact and not self.prefixes

    def label(self) -> str:
        return ", ".join([*self.exact, *(FILE_TAG + p for p in self.prefixes)]) or "the whole project (no --tags)"

    def params(self, project: str, n: int) -> dict[str, Any]:
        like = [_like(p) + "%" for p in self.prefixes]
        return {"p": project, "all": self.everything, "t": list(self.exact), "plikes": like,
                "flikes": [_like(FILE_TAG) + x for x in like], "n": n}


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_PATHS = ("jsonb_array_elements_text(CASE WHEN jsonb_typeof({0}.spec -> 'paths') = 'array' THEN {0}.spec -> 'paths'"
          " ELSE '[]'::jsonb END)")
# Pieces in scope: one of their tasks carries a matching tag or occupies a matching path.
PIECES = f"""
pieces AS (
  SELECT DISTINCT t.spec ->> 'piece' AS piece FROM benchbook.tasks t
  WHERE t.project_id = %(p)s AND 'piece' = ANY(t.tags) AND t.spec ? 'piece'
    AND (t.tags && %(t)s::text[]
         OR EXISTS (SELECT 1 FROM {_PATHS.format('t')} AS x(path) WHERE x.path LIKE ANY(%(plikes)s::text[])))
)"""
DISCARDED = f"""
WITH {PIECES}
SELECT s.id, s.tags, s.n_support, c.payload, count(*) OVER () AS total,
       (SELECT max(l.created_at) FROM benchbook.links l
        WHERE l.project_id = s.project_id AND l.dst_id = s.id AND l.relation = 'supports') AS last_at
FROM benchbook.v_claim_status s JOIN benchbook.claims c ON c.project_id = s.project_id AND c.id = s.id
WHERE s.project_id = %(p)s AND s.kind = 'observation' AND 'discarded' = ANY(s.tags) AND s.n_support > 0
  AND s.status NOT IN ('refuted', 'retracted', 'superseded')
  AND (%(all)s OR s.tags && %(t)s::text[] OR c.payload ->> 'piece' IN (SELECT piece FROM pieces)
       OR EXISTS (SELECT 1 FROM unnest(s.tags) AS x(tag) WHERE x.tag LIKE ANY(%(flikes)s::text[])))
ORDER BY last_at DESC NULLS LAST, s.created_at DESC, s.id
LIMIT %(n)s"""
FINDINGS = """
SELECT s.id, s.statement, s.tags, s.n_support, count(*) OVER () AS total
FROM benchbook.v_claim_status s
WHERE s.project_id = %(p)s AND s.kind = 'observation' AND 'out-of-scope' = ANY(s.tags)
  AND s.status IN ('proposed', 'inconclusive')
  AND (%(all)s OR s.tags && %(t)s::text[]
       OR EXISTS (SELECT 1 FROM unnest(s.tags) AS x(tag) WHERE x.tag LIKE ANY(%(flikes)s::text[])))
ORDER BY s.n_support DESC, s.created_at DESC, s.id
LIMIT %(n)s"""
OCCUPIED = f"""
SELECT f.id, f.agent, f.tags, f.spec, f.lease_left_s, count(*) OVER () AS total
FROM benchbook.v_inflight f
WHERE f.project_id = %(p)s AND f.is_intent AND 'piece' = ANY(f.tags)
  AND (%(all)s OR f.tags && %(t)s::text[]
       OR EXISTS (SELECT 1 FROM {_PATHS.format('f')} AS x(path) WHERE x.path LIKE ANY(%(plikes)s::text[])))
ORDER BY f.created_at, f.id
LIMIT %(n)s"""
_COMPLETION_SCOPE = """(%(all)s OR c.payload ->> 'piece' IN (SELECT piece FROM pieces) OR c.tags && %(t)s::text[]
         OR EXISTS (SELECT 1 FROM unnest(c.tags) AS x(tag) WHERE x.tag LIKE ANY(%(flikes)s::text[])))"""
# Completions reported and never re-run (T10): no supporting and no refuting evidence, not decided.
REPORTED = f"""
WITH {PIECES}
SELECT s.id, c.payload ->> 'piece' AS piece, c.payload ->> 'commit' AS commit, s.created_at,
       count(*) OVER () AS total
FROM benchbook.v_claim_status s JOIN benchbook.claims c ON c.project_id = s.project_id AND c.id = s.id
WHERE s.project_id = %(p)s AND s.kind = 'completion' AND s.status = 'proposed' AND s.n_support = 0
  AND s.n_refute = 0 AND {_COMPLETION_SCOPE}
ORDER BY s.created_at DESC, s.id
LIMIT %(n)s"""
# The paths a piece occupies (its newest task's spec.paths): what the validity check compares (T10).
PIECE_PATHS = """
SELECT DISTINCT ON (t.spec ->> 'piece') t.spec ->> 'piece' AS piece, t.spec -> 'paths' AS paths
FROM benchbook.tasks t
WHERE t.project_id = %s AND 'piece' = ANY(t.tags) AND t.spec ->> 'piece' = ANY(%s)
ORDER BY t.spec ->> 'piece', t.created_at DESC, t.id"""
VERIFIED = f"""
WITH {PIECES}, last AS (
  SELECT DISTINCT ON (c.payload ->> 'piece') c.payload ->> 'piece' AS piece, c.payload ->> 'commit' AS commit,
         c.payload -> 'paths' AS paths, s.last_oracle, s.id, s.decided_at
  FROM benchbook.v_claim_status s JOIN benchbook.claims c ON c.project_id = s.project_id AND c.id = s.id
  WHERE s.project_id = %(p)s AND s.kind = 'completion' AND s.status = 'verified'
    AND {_COMPLETION_SCOPE}
  ORDER BY c.payload ->> 'piece', s.decided_at DESC, s.id
)
SELECT *, count(*) OVER () AS total FROM last ORDER BY decided_at DESC, piece LIMIT %(n)s"""


def changed_scope(repo: Path, base: str | None = None) -> list[str]:
    """The brief's scope for the checkout at repo (T06, `benchbook brief --here` and the boundary hooks): the
    directories the current branch changed since its merge-base with the base ref ([guard] base, else origin/main)
    plus the working tree's changes, as file:<prefix> tags (checks.path_prefixes), and piece:<branch>. Empty on the
    base branch itself, or when the base ref or HEAD cannot be read."""
    from ... import config as localcfg
    from ...checks import DEFAULT_BASE, _git, path_prefixes

    def out(*args: str) -> str:
        return _git(repo, *args, check=False).stdout.strip()

    try:
        ref = base or localcfg.load(repo).guard_base or DEFAULT_BASE
    except Exception:  # noqa: BLE001 - an unreadable config.toml leaves the default base
        ref = DEFAULT_BASE
    head = out("rev-parse", "--verify", "--quiet", "HEAD")
    tip = out("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    branch = out("symbolic-ref", "--quiet", "--short", "HEAD")
    if not head or not tip or head == tip or branch in (ref, ref.rsplit("/", 1)[-1]):
        return []
    mb = out("merge-base", head, tip)
    if not mb:
        return []
    paths = [*out("diff", "--name-only", "--no-renames", mb, head).splitlines(),
             *out("diff", "--name-only", "--no-renames", "HEAD").splitlines(),
             *out("ls-files", "--others", "--exclude-standard").splitlines()]
    tags = [FILE_TAG + p for p in path_prefixes([p for p in paths if p])]
    return tags + ([f"piece:{branch}"] if branch else [])


def clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _total(rows: list[dict[str, Any]]) -> int:
    return int(rows[0]["total"]) if rows else 0


def _git_ok(repo: Path, *args: str) -> int | None:
    """The exit code of a git command in repo, None when it did not finish within GIT_TIMEOUT."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=GIT_TIMEOUT,
                              stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def validity(repo: Path, commit: str | None, paths: Sequence[str]) -> str | None:
    """T10: whether what was checked at commit still holds at HEAD for paths. None: unchanged (or unknown, when there
    is nothing to compare or git did not answer in time); otherwise the mark the brief line carries."""
    if not commit or not paths:
        return None
    if _git_ok(repo, "cat-file", "-e", f"{commit}^{{commit}}") != 0:
        return "commit not in this checkout"
    if _git_ok(repo, "diff", "--quiet", commit, "HEAD", "--", *paths) != 1:
        return None
    try:
        n = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", f"{commit}..HEAD", "--", *paths],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        n = ""
    return f"changed since ({n} commit{'' if n == '1' else 's'})" if n.isdigit() else "changed since"


def test_files(sig: str | None) -> list[str]:
    """The test files a signature names: the part before :: of its test ids (pytest's dotted class names become
    module paths)."""
    out: list[str] = []
    for m in _TEST_FILE.finditer(sig or ""):
        token = m.group(1).split(":")[-1]
        if "/" not in token and not token.endswith(".py") and "." in token:
            token = token.replace(".", "/") + ".py"
        if token and token not in out:
            out.append(token)
    return out


def compile_brief(lg: Ledger, task_id: UUID | None, budget_tokens: int = 2000, *, now: datetime | None = None,
                  log: bool = True, partition: str | None = None, tags: Sequence[str] | None = None,
                  repo: Path | None = None, max_chars: int | None = None) -> str:
    """partition is accepted for the compiler interface and not read: a code project's partition is the piece, and
    the brief is scoped by tags. repo: the checkout whose HEAD the validity marks compare with (T10); max_chars: a
    character limit applied with the token budget (the boundary hooks pass 6,000)."""
    from ...lookup import checked

    limit = int(budget_tokens) * 105 // 100
    if limit < 1:
        raise ValidationFailed(["budget_tokens: must be positive"])
    p = lg.project
    scope = Scope.of(tags)
    limits = {**LIMITS, **{k: int(v) for k, v in lg.pack.brief_limits.items() if k in LIMITS}}
    labels = lg.pack.brief_labels
    params = scope.params(p.id, FETCH)
    with lg._conn() as conn:
        if now is None:
            now = conn.execute("SELECT now() AS t").fetchone()["t"]
        assert now is not None
        task = None
        if task_id is not None:
            task = conn.execute("SELECT id, title, tags FROM benchbook.tasks WHERE id=%s", (task_id,)).fetchone()
        discarded = conn.execute(DISCARDED, params).fetchall()
        findings = conn.execute(FINDINGS, params).fetchall()
        occupied = conn.execute(OCCUPIED, params).fetchall()
        verified = conn.execute(VERIFIED, params).fetchall()
        reported = conn.execute(REPORTED, params).fetchall()
        piece_paths: dict[str, list[str]] = {}
        if repo is not None and verified:
            for r in conn.execute(PIECE_PATHS, (p.id, [v["piece"] for v in verified[: limits["verified"]]])):
                piece_paths[r["piece"]] = [str(x) for x in r["paths"] or [] if isinstance(x, str)]

    marks: dict[Any, str] = {}
    if repo is not None:  # T10: computed when read, never stored; at most one section's lines, 2 s per git call
        for r in verified[: limits["verified"]]:
            paths = piece_paths.get(r["piece"]) or [str(x) for x in r["paths"] or [] if isinstance(x, str)]
            if (m := validity(repo, r["commit"], paths)) is not None:
                marks[r["id"]] = m
        stale = []
        for r in discarded[: limits["discarded"]]:
            pl = r["payload"] or {}
            files = test_files(pl.get("signature"))
            if files and validity(repo, pl.get("commit"), files) not in (None, "commit not in this checkout"):
                marks[r["id"]] = "test changed since"
                stale.append(r)
        if stale:  # an approach whose failing test changed since goes to the end of its section
            discarded = [r for r in discarded if r not in stale] + stale

    head = [f"# benchbook brief · {p.id} · {now.astimezone(UTC):%Y-%m-%d %H:%MZ}", f"Goal: {clip(p.goal, 300)}",
            f"Scope: {clip(scope.label(), 300)}"]
    if task is not None:
        t_tags = f" · tags: {clip(', '.join(task['tags']), 200)}" if task["tags"] else ""
        head.append(f'Your task {short("task", task["id"])}: "{clip(task["title"], 200)}"{t_tags}')

    def mark(r: dict[str, Any]) -> str:
        return f" · {marks[r['id']]}" if r["id"] in marks else ""

    def discarded_line(r: dict[str, Any]) -> str:
        pl = r["payload"] or {}
        approach = pl.get("approach") or "(no approach named)"
        first = f" · first {short('experiment', pl['first_attempt'])}" if pl.get("first_attempt") else ""
        when = f", last {age(now, r['last_at'])}" if r["last_at"] else ""
        return (f"- {short('claim', r['id'])} {clip(approach, 60)} on {clip(pl.get('piece', '?'), 60)}: "
                f"{clip(pl.get('signature', ''), 160)}{first} · ×{r['n_support']}{when}{mark(r)}")

    def finding_line(r: dict[str, Any]) -> str:
        files = [t[len(FILE_TAG):] for t in r["tags"] if t.startswith(FILE_TAG)]
        where = f" ({clip(', '.join(files[:3]), 120)}{' …' if len(files) > 3 else ''})" if files else ""
        reports = f" · {r['n_support']} reports" if r["n_support"] > 1 else ""
        return f'- {short("claim", r["id"])} "{clip(r["statement"], 160)}"{where}{reports}'

    def occupied_line(r: dict[str, Any]) -> str:
        spec = r["spec"] or {}
        paths = [str(x) for x in spec.get("paths") or []]
        extra = [t for t in r["tags"] if t not in ("intent", "piece") and not t.startswith("piece:")]
        shown = ", ".join(clip(x, 80) for x in paths[:4]) + (f" (+{len(paths) - 4})" if len(paths) > 4 else "")
        tags_s = f" [{clip(', '.join(extra), 80)}]" if extra else ""
        return (f"- {short('task', r['id'])} {clip(spec.get('piece', '?'), 60)} by {r['agent']}, "
                f"{max(0, r['lease_left_s']) // 60}m left{tags_s}: {shown or '(no paths)'}")

    def verified_line(r: dict[str, Any]) -> str:
        return (f"- {clip(r['piece'] or '?', 60)} at {str(r['commit'] or '?')[:12]} ({short('claim', r['id'])}, "
                f"verified {age(now, r['decided_at'])}, checked: {checked(r['last_oracle'], labels)}){mark(r)}")

    def reported_line(r: dict[str, Any]) -> str:
        return (f"- {clip(r['piece'] or '?', 60)} at {str(r['commit'] or '?')[:12]} ({short('claim', r['id'])}, "
                f"reported {age(now, r['created_at'])}, checked: {checked(None, labels)})")

    def section(title: str, rows: list[dict[str, Any]], line: Any, key: str, hint: str) -> Section:
        s = Section(title, [line(r) for r in rows[: limits[key]]], total=_total(rows), hint=hint)
        if not rows:
            s.tail = ["- none recorded"]
        return s

    sections = [
        section("Last verified commit per piece ({shown} of {total})", verified, verified_line, "verified",
                "benchbook search --kind claim --status verified"),
        section("Reported done, not re-run ({shown} of {total})", reported, reported_line, "reported",
                "benchbook search --kind claim --status proposed"),
        section("Discarded approaches, do not repeat ({shown} of {total})", discarded, discarded_line, "discarded",
                "benchbook search --tags discarded"),
        section("Open out-of-scope findings ({shown} of {total})", findings, finding_line, "findings",
                "benchbook search --tags out-of-scope"),
        section("Occupied paths ({shown} of {total} live pieces)", occupied, occupied_line, "occupied",
                "benchbook piece list"),
    ]

    def render() -> str:
        lines = list(head)
        for s in sections:
            lines += s.render()
        return "\n".join(lines) + "\n" + FOOTER

    def over(text: str) -> bool:
        return estimate_tokens(text) > limit or (max_chars is not None and len(text) > max_chars)

    out = render()
    while over(out) and sections:
        if sections[-1].shown:
            sections[-1].shown -= 1
        else:
            sections.pop()
        out = render()
    if over(out):
        raise ValidationFailed(["budget_tokens: too small for the brief header and footer; request a larger budget"])
    if log:
        with lg._conn() as conn:
            conn.execute("INSERT INTO benchbook.brief_log(project_id, agent_id, task_id, tokens_est)"
                         " VALUES (%s,%s,%s,%s)", (p.id, lg.agent, task_id, estimate_tokens(out)))
    return out
