"""Builds bench/out/<run>/report.md (table + best-vs-tokens curve PNG + conditions)."""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

NAMES = {"A": "A baseline (text to lead)", "B": "B files", "C": "C benchbook"}


def _ms(xs: list[float]) -> str:
    xs = [x for x in xs if x is not None]
    if not xs:
        return "n/a"
    if len(xs) == 1:
        return f"{xs[0]:.4g}"
    return f"{statistics.mean(xs):.4g} ± {statistics.stdev(xs):.2g}"


def write_report(out: Path) -> Path:
    data = json.loads((out / "metrics.json").read_text())
    runs: list[dict[str, Any]] = data["runs"]
    cond = data["conditions"]
    modes = sorted({r["config"] for r in runs})
    lines = [f"# benchbook benchmark report{' (DRY RUN)' if cond.get('dry') else ''}", ""]
    if cond.get("dry"):
        lines += ["> **Dry run.** Agents are scripted and token costs are simulated with the assumptions in",
                  "> `bench/dry.py` (message drop rate, compaction threshold, fixed cost per step). These numbers",
                  "> validate the harness pipeline; they are not evidence about benchbook."
                  " Real runs: `MANUAL_QA.md`.", ""]
    lines += ["| configuration | runs | evaluations | duplicate ratio | lost results | lead context max (tokens) |"
              " tokens total | API cost (USD) | final best | best per 1k tokens |"
              " lead report tokens (estimated) | delivered worker reports |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for m in modes:
        rs = [r for r in runs if r["config"] == m]
        best_per_1k = [1000 * r["final_best"] / r["tokens_total"] for r in rs
                       if r["final_best"] is not None and r["tokens_total"]]
        lines.append(f"| {NAMES.get(m, m)} | {len(rs)} | {_ms([r['evaluations'] for r in rs])} | "
                     f"{_ms([r['duplicate_ratio'] for r in rs])} | {_ms([r['lost_results'] for r in rs])} | "
                     f"{_ms([r['lead_context_max'] for r in rs])} | {_ms([r['tokens_total'] for r in rs])} | "
                     f"{_ms([r.get('cost_usd') for r in rs])} | {_ms([r['final_best'] for r in rs])} | "
                     f"{_ms(best_per_1k)} | {_ms([r.get('lead_report_tokens') for r in rs])} | "
                     f"{_ms([r.get('delivered_worker_reports') for r in rs])} |")
    lines += ["", "Values are mean ± sample standard deviation over runs.", ""]
    if not cond.get("dry") and (apart := runs_apart(runs, cond)):
        lines += ["## Runs to read apart", "",
                  "Kept in the table above: which runs to exclude is the pre-registration's rule, not the report's.",
                  "", *apart, ""]
    png = _plot(out, runs, modes)
    if png:
        lines += ["## Best-so-far vs cumulative tokens", "", f"![curve]({png.name})", ""]
        if not cond.get("dry"):
            detailed = sum(r.get("curve_source") == "reconciled assistant usage" for r in runs)
            lines += [
                f"{detailed} of {len(runs)} runs reconcile assistant-message usage with the final total. "
                "Their steps use observed API usage checkpoints and the best evaluation completed by each "
                "checkpoint. Other runs contribute only their measured endpoint.",
                "",
            ]
    lines += ["## Metric definitions", "",
              "- duplicate ratio: evaluations whose (config, code) was evaluated at least twice / all evaluations",
              "- lost results: evaluations that ran but appear neither in the lead's final report nor in a",
              "  persistent record (ledger for C, result files for B). Real runs match individual receipt IDs,",
              "  status and evaluation data.",
              "- lead context max: largest prompt the lead saw (dry: simulated; real: max input+cache tokens of",
              "  a top-level assistant turn)", "",
              "- lead report tokens: token estimate of Agent/Task result payloads in the lead stream,",
              "  once per successful tool result. Compare delivered report counts with workers × rounds.", "",
              "## Conditions", "", "```json", json.dumps(cond, indent=2), "```", ""]
    p = out / "report.md"
    p.write_text("\n".join(lines))
    return p


DELIVERED_SHARE = 0.9  # below this share of workers × rounds, a run is listed as having run fewer workers


def _same_model(model: str, registered: str) -> bool:
    """An alias (sonnet) names the models whose id contains it (claude-sonnet-5-5)."""
    return model == registered or registered in model or model in registered


def runs_apart(runs: list[dict[str, Any]], cond: dict[str, Any]) -> list[str]:
    """One line per run whose workers were not the registered ones: a subagent model other than --model (a lead can
    pass `model` to the Agent tool, and it wins over CLAUDE_CODE_SUBAGENT_MODEL: 30-C-1 ran its 100 workers on
    Haiku), or fewer delivered worker reports than DELIVERED_SHARE of workers × rounds (30-C-2 ran 28 of 90)."""
    registered = str(cond.get("model") or "")
    workers, rounds = cond.get("workers"), cond.get("rounds")
    requested = workers * rounds if isinstance(workers, int) and isinstance(rounds, int) else None
    out = []
    for r in runs:
        why = []
        models = r.get("subagent_models") or {}
        if other := {m: n for m, n in models.items() if registered and not _same_model(m, registered)}:
            shown = ", ".join(f"{m} {n}" for m, n in sorted(other.items()))
            why.append(f"subagent messages on another model than {registered}: {shown} of {sum(models.values())}")
        params = {m: n for m, n in (r.get("agent_model_params") or {}).items()
                  if m != "(none)" and not (registered and _same_model(m, registered))}
        if params:
            why.append("the lead's Agent calls passed model: "
                       + ", ".join(f"{m} ×{n}" for m, n in sorted(params.items())))
        delivered = r.get("delivered_worker_reports")
        if requested and isinstance(delivered, int) and delivered < DELIVERED_SHARE * requested:
            why.append(f"{delivered} delivered worker reports of {requested} requested (workers × rounds)")
        if why:
            out.append(f"- {r['config']}-{r['run']}: " + "; ".join(why))
    return out


def _plot(out: Path, runs: list[dict[str, Any]], modes: list[str]) -> Path | None:
    if not any(r["curve"] for r in runs):
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    colors = {"A": "#c0504d", "B": "#9b8b2e", "C": "#2f6fed"}
    for m in modes:
        for i, r in enumerate(x for x in runs if x["config"] == m and x["curve"]):
            xs, ys = zip(*r["curve"], strict=True)
            style = {"color": colors.get(m), "alpha": 0.8, "label": NAMES.get(m, m) if i == 0 else None}
            if len(xs) == 1:
                ax.plot(xs, ys, marker="o", **style)
            else:
                ax.step(xs, ys, where="post", **style)
    ax.set_xlabel("cumulative tokens")
    ax.set_ylabel("best sum_radii")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    p = out / "curve.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p


if __name__ == "__main__":
    print(write_report(Path(sys.argv[1])))
