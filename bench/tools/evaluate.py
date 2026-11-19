"""Evaluation wrapper for real (Claude Code) bench runs. Every call is logged to $BENCH_EVALS.

    python tools/evaluate.py --seed 0 --agent worker-1 \
        --config '{"init":"hex","optimizer":"slsqp","iters":300,"perturb":0.01}'

Prints an identified result with config, code_ref, seed, run_status, value and notes.
Config keys: init (grid|hex|random|lattice_perturb),
optimizer (slsqp|penalty), iters (10..5000), perturb (0..0.2).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["BENCH_ROOT"])
from bench.common import SPACE, EvalLog  # noqa: E402


def config_arg(raw: str) -> dict:
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a JSON configuration object") from exc
    if not isinstance(cfg, dict) or set(cfg) - SPACE.keys():
        raise argparse.ArgumentTypeError("configuration keys are init, optimizer, iters, and perturb")
    cfg.setdefault("perturb", 0.0)
    if (cfg.get("init") not in SPACE["init"] or cfg.get("optimizer") not in SPACE["optimizer"]
            or type(cfg.get("iters")) is not int or not 10 <= cfg["iters"] <= 5000
            or type(cfg["perturb"]) not in (float, int) or not 0 <= cfg["perturb"] <= 0.2):
        raise argparse.ArgumentTypeError("invalid configuration value; see --help")
    return cfg

ap = argparse.ArgumentParser()
ap.add_argument("--config", type=config_arg, required=True,
                help="JSON object with init (grid|hex|random|lattice_perturb), optimizer (slsqp|penalty), "
                     "iters (10..5000), perturb (0..0.2; default 0)")
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--agent", default="unknown")
a = ap.parse_args()
log = EvalLog(Path(os.environ["BENCH_EVALS"]))
try:
    row = log.evaluate(a.agent, a.config, a.seed, tokens_so_far=-1)
finally:
    log.close()
print(json.dumps({key: row[key] for key in ("eval_id", "config", "code_ref", "seed", "run_status", "value", "notes")}))
