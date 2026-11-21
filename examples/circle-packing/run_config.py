"""Score one packing.py configuration. This is the command a worker runs through `benchbook exp run`:

    benchbook exp run --config '{"init": "hex", "optimizer": "slsqp", "iters": 300}' --seed 0 \
        --code-ref packing-v1 -- python run_config.py

The config and seed come from BENCHBOOK_EXP_CONFIG and BENCHBOOK_EXP_SEED, which `benchbook exp run` sets, so the
command never repeats the config and cannot disagree with the one recorded. Without benchbook:
python run_config.py --config '<json>' --seed N.

The circles are re-checked by evaluator.score_circles, never trusted. One JSON line in evaluator.py's format:
{"sum_radii": ..., "max_violation": ...} or {"invalid": reason, "run_status": "invalid"}. A config packing.py
does not know is a crash (exit 1), which exp run records as an error with the traceback in notes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluator import score_circles  # noqa: E402
from packing import solve  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("BENCHBOOK_EXP_CONFIG"),
                    help="JSON object (default: $BENCHBOOK_EXP_CONFIG)")
    ap.add_argument("--seed", type=int, default=int(os.environ.get("BENCHBOOK_EXP_SEED") or 0),
                    help="default: $BENCHBOOK_EXP_SEED, else 0")
    args = ap.parse_args()
    if not args.config:
        ap.error("no config: pass --config or run through benchbook exp run")
    circles, info = solve(json.loads(args.config), args.seed)
    res = score_circles(circles)
    if "invalid" in res:
        res["run_status"] = "invalid"
    elif info.get("converged") is False:
        res["notes"] = f"SLSQP did not converge: {info.get('message')}"
    print(json.dumps(res))


if __name__ == "__main__":
    main()
