"""Context brief compiler (spec §8): a deterministic, budgeted markdown digest. No LLM calls."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .ids import short
from .tokens import estimate_tokens

if TYPE_CHECKING:
    from .ledger import Ledger

MIN_BUDGET = 300
TRIED_SCAN = 2000  # most recent dedup groups considered for relevance


@dataclass
class Section:
    title: str  # may contain {shown} and {total}
    items: list[str]
    total: int | None = None  # total available items, if more than len(items) exist in the DB
    hint: str = "assay_search"
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


def compile_brief(lg: Ledger, task_id: UUID | None, budget_tokens: int = 2000, *,
                  now: datetime | None = None, log: bool = True) -> str:
    budget = max(MIN_BUDGET, int(budget_tokens))
    p = lg.project
    limits = {"verified": 10, "supported": 5, "tried": 8, "open_questions": 5, "recent": 3}
    limits.update({k: int(v) for k, v in lg.pack.brief_limits.items()})
    with lg._conn() as conn:
        if now is None:
            now = conn.execute("SELECT now() AS t").fetchone()["t"]
        assert now is not None
        task = None
        if task_id is not None:
            task = conn.execute("SELECT * FROM assay.tasks WHERE id=%s", (task_id,)).fetchone()
        best = conn.execute("SELECT * FROM assay.v_best WHERE project_id=%s", (p.id,)).fetchone()
        improvements = conn.execute(
            "SELECT id, author, primary_value, created_at FROM assay.experiments_valid WHERE project_id=%s"
            " AND outcome='improved' AND id <> %s ORDER BY created_at DESC, id LIMIT 2",
            (p.id, best["id"] if best else None)).fetchall()
        verified = conn.execute(
            "SELECT * FROM assay.v_claim_status WHERE project_id=%s AND status='verified'"
            " ORDER BY decided_at DESC, id", (p.id,)).fetchall()
        supported = conn.execute(
            "SELECT * FROM assay.v_claim_status WHERE project_id=%s AND status='supported'"
            " ORDER BY decided_at DESC, id", (p.id,)).fetchall()
        open_q = conn.execute(
            "SELECT * FROM assay.v_claim_status WHERE project_id=%s AND status='proposed' AND kind='hypothesis'"
            " AND n_support=0 ORDER BY created_at DESC, id", (p.id,)).fetchall()
        tried = conn.execute(
            "SELECT * FROM assay.v_tried WHERE project_id=%s ORDER BY last_at DESC, dedup_key LIMIT %s",
            (p.id, TRIED_SCAN)).fetchall()
        key_tags: dict[str, set[str]] = {}
        if task is not None and task["tags"]:
            for r in conn.execute(
                "SELECT e.dedup_key, array_agg(DISTINCT tg) AS tags FROM assay.experiments_valid e"
                " JOIN assay.tasks t ON t.id = e.task_id, unnest(t.tags) tg WHERE e.project_id=%s"
                " GROUP BY e.dedup_key", (p.id,)):
                key_tags[r["dedup_key"]] = set(r["tags"])
        last = conn.execute(
            "SELECT created_at FROM assay.brief_log WHERE project_id=%s AND agent_id=%s"
            " ORDER BY id DESC LIMIT 1", (p.id, lg.agent)).fetchone()
        since = None
        if last is not None:
            t0 = last["created_at"]
            since = conn.execute(
                "SELECT (SELECT count(*) FROM assay.experiments WHERE project_id=%(p)s AND created_at > %(t)s) AS exps,"
                " (SELECT count(*) FROM assay.experiments_valid WHERE project_id=%(p)s AND created_at > %(t)s"
                "   AND outcome='improved') AS bests,"
                " (SELECT count(*) FROM assay.claims WHERE project_id=%(p)s AND created_at > %(t)s) AS claims,"
                " (SELECT count(*) FROM assay.decisions WHERE project_id=%(p)s AND created_at > %(t)s) AS decisions",
                {"p": p.id, "t": t0}).fetchone()
            recent = conn.execute(
                "SELECT kind, id, agent, summary FROM assay.v_activity WHERE project_id=%s AND created_at > %s"
                " AND kind IN ('experiment','claim','decision') AND agent IS DISTINCT FROM %s"
                " ORDER BY created_at DESC, id LIMIT %s", (p.id, t0, lg.agent, limits["recent"])).fetchall()

    # ---------------------------------------------------------------- header
    head = [f"# assay brief · {p.id} · {now.astimezone(UTC):%Y-%m-%d %H:%MZ}", f"Goal: {p.goal}"]
    if p.primary_metric:
        b = (f"Best: {fmt(best['primary_value'])} ({short('experiment', best['id'])}, {best['author']}, "
             f"{age(now, best['created_at'])})") if best else "Best: none yet"
        head.append(f"Metric: {p.primary_metric} ({p.metric_direction}) · {b}")
    else:
        head.append("Metric: none (unscored project)")
    if task is not None:
        spec = task["spec"] or {}
        head += ["", f"## Your task {short('task', task['id'])}",
                 f'"{task["title"]}"' + (f" · tags: {', '.join(task['tags'])}" if task["tags"] else "")]
        spec_s = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if spec and spec_s != "{}":
            head.append("Spec: " + (spec_s if len(spec_s) <= 400 else spec_s[:399] + "…"))

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
                            hint="assay_search or assay_check_tried"))
    sections.append(Section("Open questions (no evidence yet)", [
        f'- {short("claim", r["id"])} "{r["statement"]}"' for r in open_q[: limits["open_questions"]]],
        total=len(open_q)))
    if since is not None:
        line = (f"{since['exps']} new experiments, {since['bests']} new best, {since['claims']} new claims, "
                f"{since['decisions']} decisions")
        pfx = {"experiment": "experiment", "claim": "claim", "decision": "decision"}
        sections.append(Section("Since your last brief", [
            f"- {short(pfx[r['kind']], r['id'])} {r['agent']}: {r['summary']}" for r in recent], tail=[line],
            hint="assay_search"))
    else:
        sections.append(Section("Since your last brief", [], tail=["(first brief for " + lg.agent + ")"]))

    foot = ["", "Details: assay_get(<id>). Before any evaluation call assay_check_tried; after it, "
                "assay_record_experiment (failures too)."]

    def render() -> str:
        lines = list(head)
        for s in sections:
            lines += s.render()
        return "\n".join(lines + foot) + "\n"

    # ---------------------------------------------------------------- budget: trim items bottom-up
    out = render()
    limit = int(budget * 1.05)
    while estimate_tokens(out) > limit:
        victim = next((s for s in reversed(sections) if s.shown > 0), None)
        if victim is None:
            break
        victim.shown -= 1
        out = render()
    if log:
        with lg._conn() as conn:
            conn.execute("INSERT INTO assay.brief_log(project_id, agent_id, task_id, tokens_est) VALUES (%s,%s,%s,%s)",
                         (p.id, lg.agent, task_id, estimate_tokens(out)))
    return out
