"""Post-hoc re-runs for the launch experiment (§13.3 metrics 1 and 5). Needs the GPU in real mode.

1. Each arm's final best code is re-run --repeats times on the same hardware (arm A: the best "keep" over all
   its branches, valued by what the shim saw it print; arm B: the verified best, else the best candidate).
2. Metric 5, the same procedure for both arms (B2): up to --sample random non-baseline keeps per arm (A: "keep"
   rows of results.tsv; B: runs that printed KEEP) are re-run --repeats times each. A keep is not reproduced
   when its re-run mean is worse than the value it reported by more than epsilon (arm B's calibrated epsilon
   for the partition, else --epsilon).

    python -m bench.autoresearch.post_replicate bench/out/autoresearch-... --gpu 0            # prints the plan
    python -m bench.autoresearch.post_replicate bench/out/autoresearch-... --gpu 0 --execute  # runs it
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any

from assay.integrations.autoresearch.checkout import Checkout
from assay.integrations.autoresearch.code import code_hash
from assay.integrations.autoresearch.ops import _isolated_dir
from assay.integrations.autoresearch.runner import run_command

from . import arms


def b_best(lg: Any) -> tuple[bytes | None, dict[str, Any]]:
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


def check_keeps(keeps: list[tuple[dict[str, Any], bytes | None]], tree: Path, eps: float, args: Any, cmd: str,
                env: dict[str, str]) -> tuple[list[dict[str, Any]], float | None]:
    checked = []
    for k, src in keeps:
        vals = rerun(tree, src, args.repeats, cmd, args.timeout, env) if src else []
        ok = [v for v in vals if v is not None]
        mean = statistics.fmean(ok) if ok else None
        checked.append({**{x: str(v) if isinstance(v, Path) else v for x, v in k.items()}, "reruns": vals,
                        "mean": mean, "reproduced": mean is not None and mean <= k["val_bpb"] + eps})
    ratio = sum(not x["reproduced"] for x in checked) / len(checked) if checked else None
    return checked, ratio


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
    from assay import Ledger

    out = args.out.resolve()
    cond = json.loads((out / "conditions.json").read_text())
    simulated = cond.get("label") == "pipeline check, not evidence"
    if (args.sim or args.execute) and args.sim != simulated:
        ap.error("post-replication mode must match the launch's real or simulated conditions")
    base = cond.get("baseline_code_hash")
    keeps = arms.a_keeps(out)
    matched = [k for k in keeps if k["source"] == "shim"]
    unmatched = len(keeps) - len(matched)
    lg = Ledger.open(project=cond["project_b"], agent="human")
    b_src, b_info = b_best(lg)
    eps = b_info.get("epsilon") or args.epsilon
    a_best = arms.a_best(matched) if not unmatched else None
    rng = random.Random(args.seed)
    a_cands = [k for k in matched if not k["baseline"] and k["code_hash"] != base] if not unmatched else []
    a_sample = rng.sample(a_cands, min(args.sample, len(a_cands)))
    b_cands = arms.b_keeps(lg, base)
    b_sample = rng.sample(b_cands, min(args.sample, len(b_cands)))
    runs = (int(a_best is not None) + int(b_src is not None) + len(a_sample) + len(b_sample)) * args.repeats
    plan = {"a_best": a_best and {k: str(v) if isinstance(v, Path) else v for k, v in a_best.items()},
            "b_best": b_info, "sampled_keeps": {"A": len(a_sample), "B": len(b_sample)}, "epsilon": eps,
            "a_unmatched_keeps": unmatched, "training_runs": runs}
    if not (args.sim or args.execute):
        print(json.dumps(plan, indent=2))
        print(f"{runs} training runs on GPU {args.gpu} (~{runs * 5.5:.0f} GPU minutes). Re-run with --execute.")
        return 0
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=str(args.gpu))
    if args.sim:
        env.setdefault("AR_SIM_SECONDS", "0.2")
    cmd = f"{sys.executable} train.py" if args.sim else args.cmd
    tree0 = Path(cond["agents"][0]["cwd"])
    result: dict[str, Any] = {"epsilon": eps, "label": cond.get("label"), "protocol": {
        "cmd": cmd, "repeats": args.repeats, "sample": args.sample, "epsilon_floor": args.epsilon,
        "seed": args.seed, "timeout": args.timeout, "gpu": None if args.sim else args.gpu}}
    if a_best is not None:
        src = arms.code_at(a_best["tree"], a_best["commit"])
        vals = rerun(tree0, src, args.repeats, cmd, args.timeout, env) if src else []
        ok = [v for v in vals if v is not None]
        result["A"] = {"reported": a_best["val_bpb"], "source": a_best["source"], "commit": a_best["commit"],
                       "code": code_hash(src or b""), "reruns": vals, "mean": statistics.fmean(ok) if ok else None}
    if b_src is not None:
        vals = rerun(tree0, b_src, args.repeats, cmd, args.timeout, env)
        ok = [v for v in vals if v is not None]
        result["B"] = {**b_info, "code": code_hash(b_src), "reruns": vals, "mean": statistics.fmean(ok) if ok else None}
    result["A_keeps"], result["A_false_best_ratio"] = check_keeps(
        [(k, arms.code_at(k["tree"], k["commit"])) for k in a_sample], tree0, eps, args, cmd, env)
    result["B_keeps"], result["B_false_best_ratio"] = check_keeps(
        [(k, lg.get_blob(k["uri"]) if k["uri"] else None) for k in b_sample], tree0, eps, args, cmd, env)
    (out / "post_replicate.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("A_keeps", "B_keeps")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
