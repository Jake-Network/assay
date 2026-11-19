"""Builds bench/out/<run>/report.md (table + best-vs-tokens curve PNG + conditions)."""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

NAMES = {"A": "A baseline (text to lead)", "B": "B files", "C": "C assay"}


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
    lines = [f"# assay benchmark report{' (DRY RUN)' if cond.get('dry') else ''}", ""]
    if cond.get("dry"):
        lines += ["> **Dry run.** Agents are scripted and token costs are simulated with the assumptions in",
                  "> `bench/dry.py` (message drop rate, compaction threshold, fixed cost per step). These numbers",
                  "> validate the harness pipeline; they are not evidence about assay. Real runs: `MANUAL_QA.md`.", ""]
    lines += ["| configuration | runs | evaluations | duplicate ratio | lost results | lead context max (tokens) |"
              " tokens total | final best |", "|---|---|---|---|---|---|---|---|"]
    for m in modes:
        rs = [r for r in runs if r["config"] == m]
        lines.append(f"| {NAMES.get(m, m)} | {len(rs)} | {_ms([r['evaluations'] for r in rs])} | "
                     f"{_ms([r['duplicate_ratio'] for r in rs])} | {_ms([r['lost_results'] for r in rs])} | "
                     f"{_ms([r['lead_context_max'] for r in rs])} | {_ms([r['tokens_total'] for r in rs])} | "
                     f"{_ms([r['final_best'] for r in rs])} |")
    lines += ["", "Values are mean ± sample standard deviation over runs.", ""]
    png = _plot(out, runs, modes)
    if png:
        lines += ["## Best-so-far vs cumulative tokens", "", f"![curve]({png.name})", ""]
    lines += ["## Metric definitions", "",
              "- duplicate ratio: evaluations whose (config, code, seed) was evaluated 2+ times / all evaluations",
              "- lost results: evaluations that ran but appear neither in the lead's final report nor in a",
              "  persistent record (ledger for C, result files for B)",
              "- lead context max: largest prompt the lead saw (dry: simulated; real: max input+cache tokens of",
              "  a top-level assistant turn)", "",
              "## Conditions", "", "```json", json.dumps(cond, indent=2), "```", ""]
    p = out / "report.md"
    p.write_text("\n".join(lines))
    return p


def _plot(out: Path, runs: list[dict[str, Any]], modes: list[str]) -> Path | None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    fig, ax = plt.subplots(figsize=(7, 4))
    colors = {"A": "#c0504d", "B": "#9b8b2e", "C": "#2f6fed"}
    for m in modes:
        for i, r in enumerate(x for x in runs if x["config"] == m):
            if r["curve"]:
                xs, ys = zip(*r["curve"], strict=True)
                ax.step(xs, ys, where="post", color=colors.get(m), alpha=0.8,
                        label=NAMES.get(m, m) if i == 0 else None)
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
