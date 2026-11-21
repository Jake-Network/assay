"""The ml-research brief (§6.7): what a GPU agent needs before its next experiment, ~1,500 tokens.

Deterministic given (ledger state, agent, partition, now). Sections are trimmed bottom-up to the budget;
the header and the footer always stay.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ...brief import Section, age, fmt
from ...errors import ValidationFailed
from ...ids import short
from ...tokens import estimate_tokens
from .code import knob_text, short_hash

if TYPE_CHECKING:
    from ...ledger import Ledger

FOOTER = ("\nNext: benchbook ar replicate --auto → benchbook ar sync → pick an idea not in progress or in the"
          " knob history"
          " → benchbook ar claim \"<idea>\" --knob K=V → edit train.py, git commit → benchbook ar run -m \"<idea>\"\n")


def _value_label(r: dict[str, Any]) -> str:
    parts = []
    if r["n_verified"]:
        parts.append("improved(verified)")
    if r["n_refuted"]:
        parts.append("improved(refuted)" + (f" ×{r['n_refuted']}" if r["n_refuted"] > 1 else ""))
    if r["n_inconclusive"]:
        parts.append("improved(inconclusive)")
    rest_improved = r["n_improved"] - r["n_verified"] - r["n_refuted"] - r["n_inconclusive"]
    for key, n in (("improved(pending)", rest_improved), ("no_change", r["n_no_change"]),
                   ("worse", r["n_worse"]), ("crash", r["n_failed"])):
        if n > 0:
            parts.append(key + (f" ×{n}" if n > 1 else ""))
    return " ".join(parts) or "tried"


def compile_brief(lg: Ledger, task_id: UUID | None, budget_tokens: int = 1500, *, now: datetime | None = None,
                  log: bool = True, partition: str | None = None) -> str:
    limit = int(budget_tokens) * 105 // 100
    if limit < 1:
        raise ValidationFailed(["budget_tokens: must be positive"])
    p = lg.refresh()
    pid = p.id
    with lg._conn() as conn:
        if now is None:
            now = conn.execute("SELECT now() AS t").fetchone()["t"]
        assert now is not None
        if partition is None and task_id is not None:
            t = conn.execute("SELECT spec FROM benchbook.tasks WHERE id=%s", (task_id,)).fetchone()
            partition = (t["spec"] or {}).get("partition") if t else None
        if partition is None:
            r = conn.execute("SELECT config->>'partition' AS p, count(*) AS n FROM benchbook.experiments_valid"
                             " WHERE project_id=%s AND config ? 'partition' GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1",
                             (pid,)).fetchone()
            partition = r["p"] if r else None
        best = conn.execute("SELECT * FROM benchbook.v_partition_best WHERE project_id=%s AND partition=%s"
                            " AND status='verified' AND rank=1", (pid, partition)).fetchone()
        best_knobs: dict[str, Any] = {}
        if best is not None and best["origin_id"] is not None:
            row = conn.execute("SELECT config->'knobs' AS k FROM benchbook.experiments WHERE id=%s",
                               (best["origin_id"],)).fetchone()
            best_knobs = (row["k"] if row else None) or {}
        cands = conn.execute(
            "SELECT b.*, e.config->'changed' AS changed, e.config->>'description' AS description,"
            " (SELECT count(*) FROM benchbook.tasks t WHERE t.project_id=b.project_id"
            " AND t.spec->>'origin'=b.ref_id::text"
            "   AND t.status='done') AS reps_done,"
            " (SELECT count(*) FROM benchbook.tasks t WHERE t.project_id=b.project_id"
            " AND t.spec->>'origin'=b.ref_id::text"
            " ) AS reps_total"
            " FROM benchbook.v_partition_best b JOIN benchbook.experiments e ON e.id=b.ref_id"
            " WHERE b.project_id=%s AND b.partition=%s AND b.status='candidate' ORDER BY b.rank LIMIT 10",
            (pid, partition)).fetchall()
        fly = conn.execute(
            "SELECT * FROM benchbook.v_inflight WHERE project_id=%s AND partition=%s AND is_intent AND agent <> %s"
            " ORDER BY created_at, id", (pid, partition, lg.agent)).fetchall()
        knobs = conn.execute(
            "SELECT * FROM benchbook.v_knob_history WHERE project_id=%s AND partition=%s ORDER BY knob, last_at DESC",
            (pid, partition)).fetchall()
        structural = conn.execute(
            "SELECT id, author, outcome, primary_value, config->>'description' AS description, created_at"
            " FROM benchbook.experiments_valid WHERE project_id=%s AND config->>'partition'=%s AND coalesce(seed,0)=0"
            " AND config->>'parent' IS NOT NULL AND (config->'changed' IS NULL OR config->'changed' = '{}'::jsonb)"
            " ORDER BY created_at DESC, id LIMIT 20", (pid, partition)).fetchall()
        claims = conn.execute(
            "SELECT * FROM benchbook.v_claim_status WHERE project_id=%s AND kind IN ('observation','hypothesis')"
            " AND status NOT IN ('refuted','retracted','superseded') AND NOT ('suspicious' = ANY(tags))"
            " ORDER BY created_at DESC, id LIMIT 40", (pid,)).fetchall()
        counts = conn.execute(
            "SELECT count(*) AS runs, count(*) FILTER (WHERE run_status<>'ok') AS failed"
            " FROM benchbook.experiments_valid"
            " WHERE project_id=%s AND config->>'partition'=%s", (pid, partition)).fetchone()

    cal = (p.settings.get("partitions") or {}).get(partition) if partition else None
    floor = float(p.settings.get("epsilon_floor", p.improve_epsilon))
    if isinstance(cal, dict):
        sig = "n/a" if cal.get("sigma") is None else f"{cal['sigma']:.6f}"
        noise = f"noise sigma={sig} → epsilon={cal['epsilon']:.6f} (calibrated, n={cal.get('n')})"
    else:
        noise = f"calibrating: epsilon={floor:g} (floor) until 3 runs of the first code agree"
    head = [f"# benchbook brief · {pid} · {now.astimezone(UTC):%Y-%m-%d %H:%MZ} · you: {lg.agent}",
            f"Goal: {p.goal}",
            f"Partition: {partition or '(none yet: run the baseline first)'} · {noise}",
            f"Metric: {p.primary_metric} ({p.metric_direction}) · {counts['runs']} runs here, "
            f"{counts['failed']} failed"]

    sections: list[Section] = []
    if best is not None:
        sd = f" ± {best['std']:.6f}" if best["std"] is not None else ""
        sections.append(Section("Verified best", [], tail=[
            f"{fmt(best['mean'])}{sd} (n={best['n']}) · code {short_hash(best['code_ref'])} · by {best['author']} · "
            f"{age(now, best['created_at'])} · {short('claim', best['ref_id'])}",
            "Run 'benchbook ar sync' to build on it (it only switches when it beats your code)."]))
    else:
        sections.append(Section("Verified best", [], tail=["none yet: the first runs are being replicated"]))

    def changed_text(ch: Any) -> str:
        if not isinstance(ch, dict) or not ch:
            return ""
        return ", ".join(f"{k} {knob_text(v[0])}→{knob_text(v[1])}" for k, v in list(ch.items())[:3])

    sections.append(Section("Candidates awaiting replication, NOT verified ({total})", [
        f"- {fmt(c['mean'])} {short('experiment', c['ref_id'])} by {c['author']}"
        + (f" · {changed_text(c['changed'])}" if changed_text(c["changed"]) else
           f" · {(c['description'] or '')[:60]}")
        + f" · replications {c['reps_done']}/{c['reps_total']}" for c in cands[:3]],
        total=len(cands), hint="benchbook ar status"))
    sections.append(Section("In progress by other agents, do not duplicate ({total})", [
        f"- {f['agent']}: \"{f['title'][:90]}\""
        + (f" · {', '.join(f'{k}={knob_text(v)}' for k, v in (f['spec'].get('knobs') or {}).items())}"
           if (f["spec"] or {}).get("knobs") else "")
        + f" · {max(0, f['lease_left_s']) // 60}m left" for f in fly[:8]], total=len(fly), hint="benchbook ar status"))

    by_knob: dict[str, list[dict[str, Any]]] = {}
    for r in knobs:
        by_knob.setdefault(r["knob"], []).append(r)
    order = sorted(by_knob, key=lambda k: (-max(x["last_at"].timestamp() for x in by_knob[k]), k))
    knob_lines = []
    for k in order[:12]:
        vals = sorted(by_knob[k], key=lambda x: -x["last_at"].timestamp())[:6]
        base = f"{knob_text(best_knobs[k])} base | " if k in best_knobs else ""
        knob_lines.append(f"- {k}: {base}" + " | ".join(f"{knob_text(v['value'])} {_value_label(v)}" for v in vals))
    sections.append(Section("Knob history ({shown} of {total} knobs, most recent first)", knob_lines,
                            total=len(by_knob), hint="benchbook sql \"SELECT * FROM v_knob_history\""))
    sections.append(Section("Code changes without knob changes (recent)", [
        f"- {s['outcome']}{' ' + fmt(s['primary_value']) if s['primary_value'] is not None else ''}: "
        f"\"{(s['description'] or '')[:90]}\" ({short('experiment', s['id'])}, {s['author']})"
        for s in structural[:5]], total=len(structural), hint="benchbook search --kind experiment"))
    obs = [c for c in claims if c["kind"] == "observation"][:3]
    hyp = [c for c in claims if c["kind"] == "hypothesis"][:3]
    sections.append(Section("Observations and ideas", [
        f"- {short('claim', c['id'])} {c['kind']}: {c['statement'][:160]}" for c in [*obs, *hyp]],
        total=len(claims), hint="benchbook claim list"))

    def render() -> str:
        lines = list(head)
        for s in sections:
            lines += s.render()
        return "\n".join(lines) + "\n" + FOOTER

    out = render()
    while estimate_tokens(out) > limit and sections:
        if sections[-1].shown:
            sections[-1].shown -= 1
        else:
            sections.pop()
        out = render()
    if estimate_tokens(out) > limit:
        raise ValidationFailed(["budget_tokens: too small for the brief header and footer; request a larger budget"])
    if log:
        with lg._conn() as conn:
            conn.execute("INSERT INTO benchbook.brief_log(project_id, agent_id, task_id, tokens_est)"
                         " VALUES (%s,%s,%s,%s)",
                         (pid, lg.agent, task_id, estimate_tokens(out)))
    return out
