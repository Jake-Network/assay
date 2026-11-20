"""Launch experiment report (§13.3): report.md (+ best_over_time.png when matplotlib is installed).

    python -m bench.autoresearch.report bench/out/autoresearch-...            # one repetition
    python -m bench.autoresearch.report bench/out/rep1 bench/out/rep2 bench/out/rep3   # + summary.md

Failed and timed-out runs are counted, never dropped. In --sim output every table is labelled as a pipeline
check.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from . import arms


def b_runs(project: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from assay import Ledger

    lg = Ledger.open(project=project, agent="human")
    rows = lg.sql_internal(
        "SELECT e.id, e.author, e.code_ref, e.seed, e.run_status, e.primary_value, e.duration_ms, e.outcome,"
        " extract(epoch FROM e.created_at) AS t, coalesce('calibration' = ANY(t.tags), false) AS calibration"
        " FROM assay.experiments_valid e LEFT JOIN assay.tasks t ON t.id = e.task_id"
        " WHERE e.project_id=%s ORDER BY e.created_at", (lg.project_id,))
    verdicts = Counter(r["status"] for r in lg.sql_internal(
        "SELECT status FROM assay.v_claim_status WHERE project_id=%s AND kind='result'", (lg.project_id,)))
    verified = lg.sql_internal(
        "SELECT extract(epoch FROM created_at) AS t, mean FROM assay.v_partition_best WHERE project_id=%s"
        " AND status='verified' ORDER BY created_at", (lg.project_id,))
    # B5: only refusals that were not followed by the same agent running that code anyway (--retry-failed);
    # 'rejected' hits are runs that already happened.
    blocked = lg.sql_internal(
        "SELECT count(*) AS n FROM assay.dedup_hits h WHERE h.project_id=%s AND h.kind='avoided'"
        " AND NOT EXISTS (SELECT 1 FROM assay.experiments x WHERE x.project_id=h.project_id"
        "   AND x.dedup_key=h.dedup_key AND x.author=h.agent_id AND coalesce(x.seed,0)=0"
        "   AND x.created_at >= h.created_at)", (lg.project_id,))
    return rows, {"verdicts": dict(verdicts), "verified": verified, "blocked": blocked[0]["n"]}


def usage(out: Path, arm: str) -> dict[str, float]:
    cost = tokens = 0.0
    for f in (out / arm).glob("agent-*.jsonl"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                cost += float(ev.get("total_cost_usd") or 0)
                u = ev.get("usage") or {}
                tokens += sum(float(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                             "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"usd": round(cost, 2), "tokens": int(tokens)}


def fmt(v: Any, nd: int = 6) -> str:
    return "n/a" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", type=Path, nargs="+", help="one output directory per repetition")
    args = ap.parse_args(argv)
    for out in args.out:
        report(out.resolve())
    if len(args.out) > 1:
        summary([o.resolve() for o in args.out])
    return 0


def summary(outs: list[Path]) -> None:
    """B6: the pre-registered comparison over repetitions: per metric, every repetition and the mean."""
    reps = [json.loads((o / "report.json").read_text()) for o in outs]
    lines = [f"# launch experiment: {len(reps)} repetitions", "", "| metric | arm | per repetition | mean |",
             "|---|---|---|---|"]
    for key in ("replicated_best", "false_best_ratio", "duplicates"):
        for arm in ("A", "B"):
            vals = [r[key][arm] for r in reps]
            ok = [float(v) for v in vals if v is not None]
            lines.append(f"| {key} | {arm} | {', '.join(fmt(v, 4) for v in vals)} | "
                         f"{fmt(sum(ok) / len(ok), 4) if ok else 'n/a'} |")
    text = "\n".join(lines) + "\n"
    (outs[0].parent / "summary.md").write_text(text, encoding="utf-8")
    print(text)


def report(out: Path) -> None:
    cond = json.loads((out / "conditions.json").read_text())
    post = json.loads((out / "post_replicate.json").read_text()) if (out / "post_replicate.json").exists() else {}
    ar, bshim = arms.load_runs(out, "A"), arms.load_runs(out, "B")
    br, binfo = b_runs(cond["project_b"])
    base = cond.get("baseline_code_hash")
    gpus = len(cond["gpus_a"])
    hours = (cond.get("minutes") or 60 * cond["hours"]) / 60
    da, db = arms.duplicates(ar, base), arms.duplicates(bshim, base)
    lost = arms.unrecorded(bshim, br)
    a_best = post.get("A", {}).get("reported")
    if a_best is None:
        best_keep = arms.a_best(arms.a_keeps(out, ar))
        a_best = best_keep["val_bpb"] if best_keep else None

    def minutes(runs: list[dict[str, Any]]) -> float:
        return sum(arms.minutes(r) for r in runs if r.get("finished"))

    def failed_minutes(runs: list[dict[str, Any]]) -> float:
        return sum(arms.minutes(r) for r in runs if r.get("finished") and not arms.is_ok(r))

    def killed(runs: list[dict[str, Any]]) -> str:
        hard = sum(1 for r in runs if not r.get("finished"))
        return f"{sum(1 for r in runs if r.get('killed'))} killed, {hard} without an end line"

    def first_runs(runs: list[dict[str, Any]]) -> int:
        return sum(1 for r in runs if arms.replication_number(r) == 0)

    b_minutes = sum((r["duration_ms"] or 0) for r in br) / 60000
    b_rep_min = sum((r["duration_ms"] or 0) for r in br if (r["seed"] or 0) > 0 and not r["calibration"]) / 60000
    b_cal_min = sum((r["duration_ms"] or 0) for r in br if r["calibration"]) / 60000
    judged = sum(binfo["verdicts"].get(k, 0) for k in ("verified", "refuted", "inconclusive"))
    restarts = cond.get("restarts") or {}
    ua, ub = usage(out, "A"), usage(out, "B")
    label = cond.get("label", "")
    nka, nkb = len(post.get("A_keeps") or []), len(post.get("B_keeps") or [])
    rows = [
        ("1. replicated best val_bpb (3 re-runs, same hardware)", fmt(post.get("A", {}).get("mean")),
         fmt(post.get("B", {}).get("mean"))),
        ("   reported best at the end", fmt(a_best) + " (best keep, as printed)",
         fmt(binfo["verified"][-1]["mean"] if binfo["verified"] else None) + " (verified mean)"),
        ("H1. runs started but not recorded", "n/a (no ledger)",
         f"{lost['not_recorded']} (+{lost['outside_assay']} started outside assay ar)"),
        ("3. duplicate runs (same code and replication number)", f"{da['duplicates']} ({da['minutes']:.1f} GPU min)",
         f"{db['duplicates']} ({db['minutes']:.1f} GPU min); {binfo['blocked']} refused before running"),
        ("   runs of the unmodified baseline (not in 3.)", str(da["baseline_runs"]), str(db["baseline_runs"])),
        ("   runs seen by the shim", f"{len(ar)} ({killed(ar)})",
         f"{len(bshim)} ({killed(bshim)}); {len(br)} in the ledger"),
        ("4. GPU min on crashes, timeouts and kills", f"{failed_minutes(ar):.1f}", f"{failed_minutes(bshim):.1f}"),
        ("5. false best ratio (keeps not reproduced)", fmt(post.get("A_false_best_ratio"), 2) + f" of {nka}",
         fmt(post.get("B_false_best_ratio"), 2) + f" of {nkb}"),
        ("   referee verdicts (not metric 5)", "n/a",
         ", ".join(f"{k} {v}" for k, v in sorted(binfo["verdicts"].items())) + f" ({judged} judged)"),
        ("6. GPU time on replication / calibration", "0% / 0%",
         f"{(b_rep_min / b_minutes if b_minutes else 0):.0%} / {(b_cal_min / b_minutes if b_minutes else 0):.0%}"),
        ("7. Claude usage", f"{ua['tokens']} tokens, USD {ua['usd']}", f"{ub['tokens']} tokens, USD {ub['usd']}"),
        ("8. experiments per GPU hour", f"{first_runs(ar) / (gpus * hours):.1f}" if hours else "n/a",
         f"{first_runs(bshim) / (gpus * hours):.1f}" if hours else "n/a"),
        ("   GPU minutes used by runs (shim)", f"{minutes(ar):.1f}", f"{minutes(bshim):.1f}"),
        ("   agent restarts", str(restarts.get("A", 0)), str(restarts.get("B", 0))),
    ]
    numbers = {"replicated_best": {"A": post.get("A", {}).get("mean"), "B": post.get("B", {}).get("mean")},
               "false_best_ratio": {"A": post.get("A_false_best_ratio"), "B": post.get("B_false_best_ratio")},
               "duplicates": {"A": da["duplicates"], "B": db["duplicates"]}, "unrecorded_B": lost,
               "restarts": restarts, "gpu_assignment": cond.get("gpu_assignment"),
               "repetition": cond.get("repetition")}
    (out / "report.json").write_text(json.dumps(numbers, indent=2, default=str), encoding="utf-8")
    a_ok = [r for r in ar if arms.is_ok(r)]
    md = [f"# autoresearch launch experiment: {cond['tag']}", "", f"**{label}**" if label else "",
          "", f"- Arms: A = independent upstream program.md, B = assay swarm (project {cond['project_b']})",
          f"- GPUs per arm: {gpus} (A {cond['gpus_a']}, B {cond['gpus_b']}); wall clock: {hours:.2f} h",
          f"- Model: {cond.get('model')}; Claude Code: {cond.get('claude_version')}; assay {cond['assay_commit'][:8]};"
          f" upstream {cond['upstream_commit'][:8]}",
          f"- Started {cond.get('started_at')}, ended {cond.get('ended_at')};"
          f" agent exit codes {cond.get('exit_codes')}",
          f"- Repetition {cond.get('repetition')}; GPU assignment {cond.get('gpu_assignment')}; restarts {restarts}",
          f"- B verdicts: {binfo['verdicts'] or 'none'}; epsilon used for keeps (both arms): {post.get('epsilon')}", "",
          "| metric | A (independent) | B (assay) |", "|---|---|---|"]
    md += [f"| {m} | {a} | {b} |" for m, a, b in rows]
    md += ["", "Metric 2 (best over time) is in best_over_time.png when matplotlib is available.",
           "Definitions are in bench/autoresearch/PREREGISTRATION.md."]
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(9, 4.5))
        t0 = min([r["start"] for r in ar] + [float(r["t"]) for r in br] or [0.0])
        best, xs, ys = None, [], []
        for r in sorted(a_ok, key=lambda r: r["end"]):
            best = r["val_bpb"] if best is None else min(best, r["val_bpb"])
            xs.append((r["end"] - t0) / 60)
            ys.append(best)
        ax.step(xs, ys, where="post", label="A reported best", color="#888")
        vx = [(float(v["t"]) - t0) / 60 for v in binfo["verified"]]
        vy: list[float] = []
        cur = None
        for v in binfo["verified"]:
            cur = v["mean"] if cur is None else min(cur, v["mean"])
            vy.append(cur)
        ax.step(vx, vy, where="post", label="B verified best", color="#1a7f37")
        cand = [r for r in br if r["outcome"] == "improved" and (r["seed"] or 0) == 0]
        ax.scatter([(float(r["t"]) - t0) / 60 for r in cand], [r["primary_value"] for r in cand],
                   facecolors="none", edgecolors="#2f6fed", label="B candidates")
        ax.set_xlabel("minutes")
        ax.set_ylabel("val_bpb")
        ax.set_title(f"best over time ({label})")
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / "best_over_time.png", dpi=120)
    except ImportError:
        md.append("(matplotlib not installed: no plot)")
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    sys.exit(main())
