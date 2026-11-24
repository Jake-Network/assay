"""Context brief compiler (spec §8): a deterministic, budgeted markdown digest. No LLM calls."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .errors import ValidationFailed
from .ids import short
from .tokens import estimate_tokens

if TYPE_CHECKING:
    from .ledger import Ledger

TRIED_SCAN = 2000  # most recent dedup groups considered for relevance
FOOTER = (
    "\nDetails: benchbook_get(<id>). Before any evaluation call benchbook_check_tried; after it, "
    "benchbook_record_experiment (failures too).\n"
)


@dataclass
class Section:
    title: str  # may contain {shown} and {total}
    items: list[str]
    total: int | None = None  # total available items, if more than len(items) exist in the DB
    hint: str = "benchbook_search"
    tail: list[str] = field(default_factory=list)  # lines that are always shown under the title
    shown: int = -1

    def __post_init__(self) -> None:
        if self.total is None:
            self.total = len(self.items)
        self.shown = len(self.items)

    def render(self) -> list[str]:
        if self.shown == 0 and not self.tail:
            return []
        lines = ["", "## " + self.title.format(shown=self.shown, total=self.total), *self.tail,
                 *self.items[: self.shown]]
        more = (self.total or 0) - self.shown
        if more > 0:
            lines.append(f"(+{more} more: {self.hint})")
        return lines


def age(now: datetime, t: datetime) -> str:
    s = max(0, int((now - t).total_seconds()))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.6g}"


def _config_line(cfg: Any, keys: list[str] | None, max_len: int = 90) -> str:
    from .ledger import config_line

    if isinstance(cfg, dict) and keys:
        core = {k: cfg[k] for k in keys if k in cfg}
        rest = len([k for k in cfg if k not in core])
        s = config_line(core, max_len) if core else config_line(cfg, max_len)
        return s + (f" (+{rest} keys)" if core and rest else "")
    return config_line(cfg, max_len)


def _clip_tokens(value: str, budget: int) -> str:
    if estimate_tokens(value) <= budget:
        return value
    remaining = (budget - 1) * 4  # reserve one token for the ellipsis
    for i, char in enumerate(value):
        remaining -= 1 if char.isascii() else 4
        if remaining < 0:
            return value[:i].rstrip() + "…"
    return "…"


def _fit_header(lines: list[tuple[str, str, str]], limit: int, best_line: str) -> list[str]:
    """Keep the required labels, while sharing available space between their values."""
    values = [value for _, value, _ in lines]
    used = estimate_tokens("\n".join(prefix + suffix for prefix, _, suffix in lines) + "\n" + FOOTER)
    available = limit - used
    variable = sorted((estimate_tokens(value), i) for i, value in enumerate(values) if value)
    if available < len(variable):
        raise ValidationFailed(["budget_tokens: too small for the brief header and footer; request a larger budget"])
    best_cost = estimate_tokens(best_line)
    if available - best_cost >= len(variable):
        available -= best_cost
    else:
        best_line = ""
    for position, (size, i) in enumerate(variable):
        allowance = min(size, available // (len(variable) - position))
        values[i] = _clip_tokens(values[i], allowance)
        available -= estimate_tokens(values[i])
    fitted = [prefix + value + suffix for (prefix, _, suffix), value in zip(lines, values, strict=True)]
    fitted[2] += best_line  # metric line
    return fitted


def compile_brief(lg: Ledger, task_id: UUID | None, budget_tokens: int = 2000, *,
                  now: datetime | None = None, log: bool = True, partition: str | None = None,
                  tags: Sequence[str] | None = None, repo: Any = None, max_chars: int | None = None) -> str:
    """tags narrow a pack brief that reads them (the code pack's: per module tag); a brief that does not read them
    refuses them rather than ignore them."""
    if lg.pack.brief_compiler:
        import importlib
        import inspect

        mod, _, fn = lg.pack.brief_compiler.partition(":")
        compiler = getattr(importlib.import_module(mod), fn)
        extra: dict[str, Any] = {}
        if tags:
            if "tags" not in inspect.signature(compiler).parameters:
                raise ValidationFailed([f"tags: the {lg.pack.name} pack's brief does not read tags"])
            extra["tags"] = list(tags)
        params = inspect.signature(compiler).parameters
        if repo is not None and "repo" in params:  # the code pack's validity marks (T10)
            extra["repo"] = repo
        if max_chars is not None and "max_chars" in params:
            extra["max_chars"] = max_chars
        return compiler(lg, task_id, budget_tokens, now=now, log=log, partition=partition,  # type: ignore[no-any-return]
                        **extra)
    if tags:
        raise ValidationFailed([f"tags: the {lg.pack.name} pack's brief does not read tags (the code pack's does)"])
    limit = int(budget_tokens) * 105 // 100
    if limit < 1:
        raise ValidationFailed(["budget_tokens: must be positive"])
    p = lg.project
    limits = {"verified": 10, "supported": 5, "tried": 8, "related": 5, "open_questions": 5, "recent": 3}
    limits.update({k: int(v) for k, v in lg.pack.brief_limits.items()})
    with lg._conn() as conn:
        if now is None:
            now = conn.execute("SELECT now() AS t").fetchone()["t"]
        assert now is not None
        task = None
        if task_id is not None:
            task = conn.execute("SELECT * FROM benchbook.tasks WHERE id=%s", (task_id,)).fetchone()
        best = conn.execute("SELECT * FROM benchbook.v_best WHERE project_id=%s ORDER BY CASE WHEN %s = 'min'"
                            " THEN primary_value ELSE -primary_value END, created_at LIMIT 1",
                            (p.id, p.metric_direction)).fetchone()
        improvements = conn.execute(
            "SELECT id, author, primary_value, created_at FROM benchbook.experiments_valid WHERE project_id=%s"
            " AND outcome='improved' AND id <> %s ORDER BY created_at DESC, id LIMIT 2",
            (p.id, best["id"] if best else None)).fetchall()
        verified = conn.execute(
            "SELECT * FROM benchbook.v_claim_status WHERE project_id=%s AND status='verified'"
            " ORDER BY decided_at DESC, id", (p.id,)).fetchall()
        supported = conn.execute(
            "SELECT * FROM benchbook.v_claim_status WHERE project_id=%s AND status='supported'"
            " ORDER BY decided_at DESC, id", (p.id,)).fetchall()
        open_q = conn.execute(
            "SELECT * FROM benchbook.v_claim_status WHERE project_id=%s AND status='proposed' AND kind='hypothesis'"
            " AND n_support=0 ORDER BY created_at DESC, id", (p.id,)).fetchall()
        tried = conn.execute(
            "SELECT * FROM benchbook.v_tried WHERE project_id=%s ORDER BY last_at DESC, dedup_key LIMIT %s",
            (p.id, TRIED_SCAN)).fetchall()
        related: list[dict[str, Any]] = []
        key_tags: dict[str, set[str]] = {}
        if task is not None and task["tags"]:
            # U2: findings other agents left under this task's tags (observations, notes, ...) before anyone
            # has judged them; they are otherwise invisible until a decision makes them supported.
            related = conn.execute(
                "SELECT s.id, s.kind, s.statement, s.author, s.created_at,"
                " cardinality(ARRAY(SELECT unnest(s.tags) INTERSECT SELECT unnest(%(t)s::text[]))) AS overlap"
                " FROM benchbook.v_claim_status s WHERE s.project_id=%(p)s AND s.status='proposed'"
                " AND s.tags && %(t)s::text[] AND NOT (s.kind = 'hypothesis' AND s.n_support = 0)"
                " ORDER BY overlap DESC, s.created_at DESC, s.id LIMIT 50",
                {"p": p.id, "t": list(task["tags"])}).fetchall()
            for r in conn.execute(
                "SELECT e.dedup_key, array_agg(DISTINCT tg) AS tags FROM benchbook.experiments_valid e"
                " JOIN benchbook.tasks t ON t.id = e.task_id, unnest(t.tags) tg WHERE e.project_id=%s"
                " GROUP BY e.dedup_key", (p.id,)):
                key_tags[r["dedup_key"]] = set(r["tags"])
        last = conn.execute(
            "SELECT created_at FROM benchbook.brief_log WHERE project_id=%s AND agent_id=%s"
            " ORDER BY id DESC LIMIT 1", (p.id, lg.agent)).fetchone()
        since = None
        if last is not None:
            t0 = last["created_at"]
            since = conn.execute(
                "SELECT (SELECT count(*) FROM benchbook.experiments_valid"
                " WHERE project_id=%(p)s AND created_at > %(t)s) AS exps,"
                " (SELECT count(*) FROM benchbook.experiments_valid WHERE project_id=%(p)s AND created_at > %(t)s"
                "   AND outcome='improved') AS bests,"
                " (SELECT count(*) FROM benchbook.claims WHERE project_id=%(p)s AND created_at > %(t)s) AS claims,"
                " (SELECT count(*) FROM benchbook.decisions WHERE project_id=%(p)s AND created_at > %(t)s)"
                " AS decisions",
                {"p": p.id, "t": t0}).fetchone()
            recent = conn.execute(
                "SELECT kind, id, agent, summary FROM benchbook.v_activity WHERE project_id=%s AND created_at > %s"
                " AND kind IN ('experiment','claim','decision') AND agent IS DISTINCT FROM %s"
                " ORDER BY created_at DESC, id LIMIT %s", (p.id, t0, lg.agent, limits["recent"])).fetchall()

    # ---------------------------------------------------------------- header
    heading = f"# benchbook brief · {p.id} · {now.astimezone(UTC):%Y-%m-%d %H:%MZ}"
    header = [(heading, "", ""), ("Goal: ", p.goal, "")]
    best_line = ""
    if p.primary_metric:
        b = (f"Best: {fmt(best['primary_value'])} ({short('experiment', best['id'])}, {best['author']}, "
             f"{age(now, best['created_at'])})") if best else "Best: none yet"
        best_line = " · " + b
        header.append(("Metric: ", p.primary_metric, f" ({p.metric_direction})"))
    else:
        header.append(("Metric: none (unscored project)", "", ""))
    head = [prefix + value + suffix for prefix, value, suffix in header]
    head[-1] += best_line
    if task is not None:
        spec = task["spec"] or {}
        task_heading = f"## Your task {short('task', task['id'])}"
        header.extend([("", "", ""), (task_heading, "", ""), ('"', task["title"], '"')])
        head += ["", task_heading, f'"{task["title"]}"']
        if task["tags"]:
            tags = ", ".join(task["tags"])
            head[-1] += f" · tags: {tags}"
            header.append(("Tags: ", tags, ""))
        if spec:
            spec_s = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            spec_s = spec_s if len(spec_s) <= 400 else spec_s[:399] + "…"
            head.append("Spec: " + spec_s)
            header.append(("Spec: ", spec_s, ""))
    if estimate_tokens("\n".join(head) + "\n" + FOOTER) > limit:
        compact_best = f" · Best: {fmt(best['primary_value'])} ({short('experiment', best['id'])})" if best else ""
        head = _fit_header(header, limit, compact_best)

    sections: list[Section] = []
    if improvements:
        sections.append(Section("Recent improvements", [
            f"- {fmt(r['primary_value'])} ({short('experiment', r['id'])}, {r['author']}, "
            f"{age(now, r['created_at'])})" for r in improvements]))

    def claim_line(r: dict[str, Any], extra: str) -> str:
        warn = " ⚠ evidence invalidated" if r["evidence_invalidated"] else ""
        return f'- {short("claim", r["id"])} "{r["statement"]}" · {extra}{warn}'

    sections.append(Section("Verified ({total})", [
        claim_line(r, f"oracle: {r['last_oracle']} · {r['n_support']} supporting")
        for r in verified[: limits["verified"]]], total=len(verified)))
    sections.append(Section("Supported, NOT verified ({total})", [
        claim_line(r, f"{r['n_support']} supporting, {r['n_refute']} refuting")
        for r in supported[: limits["supported"]]], total=len(supported)))

    # ---------------------------------------------------------------- already tried (§8.2)
    space_keys: list[str] = []
    task_tags: set[str] = set()
    if task is not None:
        task_tags = set(task["tags"] or [])
        cs = (task["spec"] or {}).get("config_space")
        if isinstance(cs, dict):
            space_keys = sorted(cs.keys())
        elif isinstance(cs, list):
            space_keys = sorted(str(k) for k in cs)

    def score(r: dict[str, Any]) -> int:
        cfg = r["config"] if isinstance(r["config"], dict) else {}
        s = 2 * len(task_tags & key_tags.get(r["dedup_key"], set())) + len(set(space_keys) & set(cfg))
        return s + (1 if r["summary_outcome"] in ("failed", "worse") else 0) if s > 0 else 0

    if task is not None and (task_tags or space_keys):
        rel = sorted(((score(r), r) for r in tried), key=lambda x: (-x[0], -x[1]["last_at"].timestamp(),
                                                                     x[1]["dedup_key"]))
        rel_rows = [r for s, r in rel if s > 0]
        label = "relevant"
    else:
        rel_rows = list(tried)
        label = "total"

    def tried_line(r: dict[str, Any]) -> str:
        oc = r["summary_outcome"]
        if oc == "improved" and r["seeds"] < 2:
            oc = "improved (n=1, unreplicated)"  # C5: one seed can be luck
        n = r["seeds"] if r["seeds"] > 1 else r["attempts"]
        unit = " seeds" if r["seeds"] > 1 else ""
        val = f" best {fmt(r['best_value'])}" if r["best_value"] is not None else ""
        note = ""
        if oc in ("failed", "worse", "no_change") and r["last_notes"]:
            note = ": " + r["last_notes"].replace("\n", " ")[:80]
        return (f"- {_config_line(r['config'], space_keys)} → {oc}{note} ×{n}{unit}{val} "
                f"({short('experiment', r['last_id'])})")

    sections.append(Section("Already tried, do not repeat ({shown} of {total} " + label + ")",
                            [tried_line(r) for r in rel_rows[: limits["tried"]]], total=len(rel_rows),
                            hint="benchbook_search or benchbook_check_tried"))
    if related:
        sections.append(Section("Related findings, not yet judged ({total})", [
            f'- {short("claim", r["id"])} {r["kind"]} by {r["author"]}: "{r["statement"]}"'
            for r in related[: limits["related"]]], total=len(related), hint="benchbook_search"))
    sections.append(Section("Open questions (no evidence yet)", [
        f'- {short("claim", r["id"])} "{r["statement"]}"' for r in open_q[: limits["open_questions"]]],
        total=len(open_q)))
    if since is not None:
        line = (f"{since['exps']} new experiments, {since['bests']} new best, {since['claims']} new claims, "
                f"{since['decisions']} decisions")
        pfx = {"experiment": "experiment", "claim": "claim", "decision": "decision"}
        sections.append(Section("Since your last brief", [
            f"- {short(pfx[r['kind']], r['id'])} {r['agent']}: {r['summary']}" for r in recent], tail=[line],
            hint="benchbook_search"))
    else:
        sections.append(Section("Since your last brief", [], tail=["(first brief for " + lg.agent + ")"]))

    def render() -> str:
        lines = list(head)
        for s in sections:
            lines += s.render()
        return "\n".join(lines) + "\n" + FOOTER

    # ---------------------------------------------------------------- budget: trim items bottom-up
    out = render()
    while estimate_tokens(out) > limit:
        if sections[-1].shown:
            sections[-1].shown -= 1
        else:
            sections.pop()
        out = render()
    if log:
        with lg._conn() as conn:
            conn.execute("INSERT INTO benchbook.brief_log(project_id, agent_id, task_id, tokens_est)"
                         " VALUES (%s,%s,%s,%s)",
                         (p.id, lg.agent, task_id, estimate_tokens(out)))
    return out
