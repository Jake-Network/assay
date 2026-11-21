"""Benchmark harness (spec §15): A baseline vs B files vs C benchbook at the same budget.

    python -m bench.run --dry                      # scripted agents, validates the whole pipeline
    python -m bench.run --model sonnet             # prints the real `claude -p` commands
    python -m bench.run --model sonnet --execute   # runs them (costs money; see MANUAL_QA.md)
    python -m bench.report bench/out/<run>         # (re)build report.md
    python -m bench.analyze_cost bench/out/<run>   # where the tokens went: cost-breakdown.md
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict
from pathlib import Path

from .report import write_report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", default="A,B,C")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--dry", action="store_true", help="scripted agents, no LLM calls")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--evals", type=int, default=5, help="evaluations per worker task")
    ap.add_argument("--budget", type=int, default=40000, help="dry: token budget per run")
    ap.add_argument("--max-budget-usd", type=float, default=5.0, help="real: Claude API dollar limit per run")
    ap.add_argument("--drop-rate", type=float, default=0.15, help="dry A: chance a worker report is never delivered")
    ap.add_argument("--compact-at", type=int, default=3000, help="dry A: lead context that triggers compaction")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-turns", type=int, default=80)
    ap.add_argument("--wall-clock", type=int, default=60, help="real: minutes per run")
    ap.add_argument("--execute", action="store_true", help="real: actually run claude -p")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    modes = args.configs.split(",")
    if any(m not in ("A", "B", "C") for m in modes) or len(set(modes)) != len(modes):
        ap.error("--configs must list distinct configurations from A,B,C")
    if min(args.runs, args.workers, args.rounds, args.evals, args.max_turns, args.wall_clock, args.budget) <= 0:
        ap.error("run limits must be positive")
    if not math.isfinite(args.max_budget_usd) or args.max_budget_usd <= 0:
        ap.error("--max-budget-usd must be a positive finite amount")

    out = Path(args.out or f"bench/out/{time.strftime('%Y%m%d-%H%M%S')}{'-dry' if args.dry else ''}").resolve()
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        ap.exit(1, f"output already exists: {out}\n")
    results = []
    for mode in modes:
        for run in range(1, args.runs + 1):
            d = out / f"{mode}-{run}"
            if args.dry:
                from .dry import run_dry

                m = run_dry(mode, d, run, args)
            else:
                from .real import BenchRunError, run_real

                try:
                    m = run_real(mode, d, run, args)
                except (BenchRunError, OSError) as exc:
                    ap.exit(1, f"{mode}-{run}: {exc}\nReview the artifacts at {d}\n")
            if m is not None:
                results.append(asdict(m))
                print(f"{mode}-{run}: evals={m.evaluations} dup={m.duplicate_ratio:.2%} lost={m.lost_results} "
                      f"lead_ctx_max={m.lead_context_max} tokens={m.tokens_total} best={m.final_best}")
    if results:
        cond = {k: v for k, v in vars(args).items() if k not in ("out",)}
        (out / "metrics.json").write_text(json.dumps({"conditions": cond, "runs": results}, indent=2))
        print(write_report(out))


if __name__ == "__main__":
    main()
