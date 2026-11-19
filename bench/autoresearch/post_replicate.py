"""Post-hoc re-runs for the launch experiment (§13.3 metrics 1 and 5). Needs the GPU in real mode.

1. Each arm's final best code is re-run 3 times on the same hardware (arm A: the best "keep" over all its
   branches; arm B: the verified best, else the best candidate).
2. Up to 10 random arm-A "keep" commits (not the baseline) are re-run 3 times each. A keep counts as not
   reproduced when the re-run mean is worse than its reported val_bpb by more than epsilon (arm B's calibrated
   epsilon for the partition, else --epsilon).

    python -m bench.autoresearch.post_replicate bench/out/autoresearch-... --gpu 0            # prints the plan
    python -m bench.autoresearch.post_replicate bench/out/autoresearch-... --gpu 0 --execute  # runs it
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from assay.integrations.autoresearch.checkout import Checkout
from assay.integrations.autoresearch.code import code_hash
from assay.integrations.autoresearch.ops import _isolated_dir
from assay.integrations.autoresearch.runner import run_command


def a_keeps(out: Path) -> list[dict[str, Any]]:
    rows = []
    for wt in sorted((out / "A").glob("gpu*")):
        f = wt / "results.tsv"
        if not f.exists():
            continue
        with open(f, encoding="utf-8", newline="") as fh:
            for i, r in enumerate(csv.DictReader(fh, delimiter="\t")):
                if r.get("status", "").strip().lower() == "keep":
                    rows.append({"tree": wt, "commit": r["commit"], "val_bpb": float(r["val_bpb"]),
                                 "description": r.get("description", ""), "baseline": i == 0})
    return rows


def code_at(tree: Path, commit: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(tree), "show", f"{commit}:./train.py"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def b_best(conditions: dict[str, Any]) -> tuple[bytes | None, dict[str, Any]]:
    from assay import Ledger

    lg = Ledger.open(project=conditions["project_b"], agent="human")
    rows = lg.sql_internal("SELECT * FROM assay.v_partition_best WHERE project_id=%s AND rank=1"
                           " ORDER BY (status='verified') DESC, mean LIMIT 1", (lg.project_id,))
    if not rows:
        return None, {}
    origin = lg.get(str(rows[0]["origin_id"]))["record"]
    uri = next((a["uri"] for a in origin["artifacts"] if str(a.get("uri", "")).startswith("assay://blob/")), None)
    eps = (lg.project.settings.get("partitions") or {}).get(rows[0]["partition"], {}).get("epsilon")
    return (lg.get_blob(uri) if uri else None), {"status": rows[0]["status"], "mean": rows[0]["mean"],
                                                 "n": rows[0]["n"], "epsilon": eps}


def rerun(tree: Path, src: bytes, n: int, cmd: str, timeout: float, env: dict[str, str]) -> list[float | None]:
    vals: list[float | None] = []
    for _ in range(n):
        tmp = _isolated_dir(Checkout(tree), src)
        try:
            res = run_command(cmd, tmp, tmp / "run.log", timeout, env=env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        vals.append(float(res.metrics["val_bpb"]) if res.run_status == "ok" else None)
    return vals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--gpu", default="0")
    ap.add_argument("--cmd", default="uv run train.py")
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--epsilon", type=float, default=0.0005, help="used when arm B has no calibration")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sim", action="store_true")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args(argv)
    out = args.out.resolve()
    cond = json.loads((out / "conditions.json").read_text())
    keeps = a_keeps(out)
    b_src, b_info = b_best(cond)
    eps = b_info.get("epsilon") or args.epsilon
    a_best = min(keeps, key=lambda r: r["val_bpb"]) if keeps else None
    candidates = [k for k in keeps if not k["baseline"]]
    sample = random.Random(args.seed).sample(candidates, min(args.sample, len(candidates)))
    runs = 3 * args.repeats + len(sample) * args.repeats
    plan = {"a_best": a_best and {k: str(v) if isinstance(v, Path) else v for k, v in a_best.items()},
            "b_best": b_info, "sampled_keeps": len(sample), "epsilon": eps, "training_runs": runs}
    if not (args.sim or args.execute):
        print(json.dumps(plan, indent=2))
        print(f"{runs} training runs on GPU {args.gpu} (~{runs * 5.5:.0f} GPU minutes). Re-run with --execute.")
        return 0
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(args.gpu))
    if args.sim:
        env.setdefault("AR_SIM_SECONDS", "0.2")
    cmd = f"{sys.executable} train.py" if args.sim else args.cmd
    tree0 = Path(cond["agents"][0]["cwd"])
    result: dict[str, Any] = {"epsilon": eps, "label": cond.get("label")}
    if a_best is not None:
        src = code_at(a_best["tree"], a_best["commit"])
        vals = rerun(tree0, src, args.repeats, cmd, args.timeout, env) if src else []
        ok = [v for v in vals if v is not None]
        result["A"] = {"reported": a_best["val_bpb"], "commit": a_best["commit"], "code": code_hash(src or b""),
                       "reruns": vals, "mean": statistics.fmean(ok) if ok else None}
    if b_src is not None:
        vals = rerun(tree0, b_src, args.repeats, cmd, args.timeout, env)
        ok = [v for v in vals if v is not None]
        result["B"] = {**b_info, "code": code_hash(b_src), "reruns": vals, "mean": statistics.fmean(ok) if ok else None}
    false_best = []
    for k in sample:
        src = code_at(k["tree"], k["commit"])
        vals = rerun(tree0, src, args.repeats, cmd, args.timeout, env) if src else []
        ok = [v for v in vals if v is not None]
        mean = statistics.fmean(ok) if ok else None
        false_best.append({"commit": k["commit"], "reported": k["val_bpb"], "reruns": vals, "mean": mean,
                           "reproduced": mean is not None and mean <= k["val_bpb"] + eps})
    result["A_keeps"] = false_best
    result["A_false_best_ratio"] = (sum(not x["reproduced"] for x in false_best) / len(false_best)
                                    if false_best else None)
    (out / "post_replicate.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "A_keeps"}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
