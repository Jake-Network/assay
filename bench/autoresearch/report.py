"""Launch experiment report (§13.3): report.md (+ best_over_time.png when matplotlib is installed).

    python -m bench.autoresearch.report bench/out/autoresearch-...

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


def a_runs(out: Path) -> list[dict[str, Any]]:
    f = out / "A" / "runs.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]


def b_runs(project: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from assay import Ledger

    lg = Ledger.open(project=project, agent="human")
    rows = lg.sql_internal("SELECT id, author, code_ref, seed, run_status, primary_value, duration_ms, outcome,"
                           " extract(epoch FROM created_at) AS t FROM assay.experiments_valid WHERE project_id=%s"
                           " ORDER BY created_at", (lg.project_id,))
    verdicts = Counter(r["status"] for r in lg.sql_internal(
        "SELECT status FROM assay.v_claim_status WHERE project_id=%s AND kind='result'", (lg.project_id,)))
    verified = lg.sql_internal(
        "SELECT extract(epoch FROM created_at) AS t, mean FROM assay.v_partition_best WHERE project_id=%s"
        " AND status='verified' ORDER BY created_at", (lg.project_id,))
    blocked = lg.sql_internal("SELECT count(*) AS n FROM assay.dedup_hits WHERE project_id=%s", (lg.project_id,))
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
    ap.add_argument("out", type=Path)
    args = ap.parse_args(argv)
    out = args.out.resolve()
    cond = json.loads((out / "conditions.json").read_text())
    post = json.loads((out / "post_replicate.json").read_text()) if (out / "post_replicate.json").exists() else {}
    ar = a_runs(out)
    br, binfo = b_runs(cond["project_b"])
    gpus = len(cond["gpus_a"])
    a_ok = [r for r in ar if r["exit"] == 0 and r.get("val_bpb") is not None]
    a_minutes = sum(r["end"] - r["start"] for r in ar) / 60
    a_fail_min = sum(r["end"] - r["start"] for r in ar if r["exit"] != 0 or r.get("val_bpb") is None) / 60
    seen: Counter[str] = Counter(r["code_hash"] for r in a_ok)
    a_dup = sum(n - 1 for n in seen.values() if n > 1)
    a_dup_min = 0.0
    firsts: set[str] = set()
    for r in a_ok:
        if r["code_hash"] in firsts:
            a_dup_min += (r["end"] - r["start"]) / 60
        firsts.add(r["code_hash"])
    b_ok = [r for r in br if r["run_status"] == "ok"]
    b_minutes = sum((r["duration_ms"] or 0) for r in br) / 60000
    b_fail_min = sum((r["duration_ms"] or 0) for r in br if r["run_status"] != "ok") / 60000
    b_rep_min = sum((r["duration_ms"] or 0) for r in br if (r["seed"] or 0) > 0) / 60000
    pairs: Counter[tuple[str, int]] = Counter((r["code_ref"], r["seed"] or 0) for r in b_ok)
    b_dup = sum(n - 1 for n in pairs.values() if n > 1)
    judged = sum(binfo["verdicts"].get(k, 0) for k in ("verified", "refuted", "inconclusive"))
    b_false = ((binfo["verdicts"].get("refuted", 0) + binfo["verdicts"].get("inconclusive", 0)) / judged
               if judged else None)
    hours = (cond.get("minutes") or 60 * cond["hours"]) / 60
    ua, ub = usage(out, "A"), usage(out, "B")
    label = cond.get("label", "")
    rows = [
        ("1. replicated best val_bpb (3 re-runs, same hardware)", fmt(post.get("A", {}).get("mean")),
         fmt(post.get("B", {}).get("mean"))),
        ("   reported best at the end", fmt(min((r["val_bpb"] for r in a_ok), default=None)),
         fmt(binfo["verified"][-1]["mean"] if binfo["verified"] else None) + " (verified mean)"),
        ("3. duplicate runs (same normalised code)", f"{a_dup} ({a_dup_min:.1f} GPU min)",
         f"{b_dup} ({binfo['blocked']} blocked before running)"),
        ("   runs recorded", f"{len(ar)} (shim log)", f"{len(br)} (ledger)"),
        ("4. GPU min on crashes and timeouts", f"{a_fail_min:.1f}", f"{b_fail_min:.1f}"),
        ("5. false best ratio", fmt(post.get("A_false_best_ratio"), 2) + " (keeps not reproduced)",
         fmt(b_false, 2) + " (refuted + inconclusive / judged)"),
        ("6. GPU time on replication", "0%", f"{(b_rep_min / b_minutes if b_minutes else 0):.0%}"),
        ("7. Claude usage", f"{ua['tokens']} tokens, USD {ua['usd']}", f"{ub['tokens']} tokens, USD {ub['usd']}"),
        ("8. experiments per GPU hour", f"{len(ar) / (gpus * hours):.1f}" if hours else "n/a",
         f"{len([r for r in br if (r['seed'] or 0) == 0]) / (gpus * hours):.1f}" if hours else "n/a"),
        ("   GPU minutes used by runs", f"{a_minutes:.1f}", f"{b_minutes:.1f}"),
    ]
    md = [f"# autoresearch launch experiment: {cond['tag']}", "", f"**{label}**" if label else "",
          "", f"- Arms: A = independent upstream program.md, B = assay swarm (project {cond['project_b']})",
          f"- GPUs per arm: {gpus} (A {cond['gpus_a']}, B {cond['gpus_b']}); wall clock: {hours:.2f} h",
          f"- Model: {cond.get('model')}; Claude Code: {cond.get('claude_version')}; assay {cond['assay_commit'][:8]};"
          f" upstream {cond['upstream_commit'][:8]}",
          f"- Started {cond.get('started_at')}, ended {cond.get('ended_at')};"
          f" agent exit codes {cond.get('exit_codes')}",
          f"- B verdicts: {binfo['verdicts'] or 'none'}; epsilon used for A keeps: {post.get('epsilon')}", "",
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
