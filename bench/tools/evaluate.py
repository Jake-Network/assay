"""Evaluation wrapper for real (Claude Code) bench runs. Every call is logged to $BENCH_EVALS.

    python tools/evaluate.py --seed 0 --agent worker-1 \
        --config '{"init":"hex","optimizer":"slsqp","iters":300,"perturb":0.01}'

Prints {"sum_radii": ...} or {"invalid": ...}. Config keys: init (grid|hex|random|lattice_perturb),
optimizer (slsqp|penalty), iters (10..5000), perturb (0..0.2).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["BENCH_ROOT"])
from bench.common import EvalLog  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--config", required=True)
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--agent", default="unknown")
a = ap.parse_args()
log = EvalLog(Path(os.environ["BENCH_EVALS"]))
row = log.evaluate(a.agent, json.loads(a.config), a.seed, tokens_so_far=-1)
log.close()
print(json.dumps({"sum_radii": row["value"]} if row["value"] is not None else {"invalid": row["notes"],
                                                                            "run_status": row["run_status"]}))
