"""Scripted circle-packing swarm: N worker processes and a scripted lead write to one assay ledger,
with no LLM calls. Used for CI, the viewer demo and `bench --dry`.

    python examples/circle-packing/scripted_swarm.py --workers 8 --minutes 5
    assay serve     # in another terminal, then open http://127.0.0.1:7466

Workers claim tasks, read their brief, check_tried before every evaluation and record every result,
failures included. Some workers are deliberately careless (they sometimes re-run a config that was already
tried) so that duplicate rejection shows up. The lead creates tasks, asks for replications of the best
configuration and marks a claim verified once the rerun rule (>= 3 seeds, >= 2 agents) holds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from assay import Ledger  # noqa: E402
from assay.errors import AssayError, LeaseLost  # noqa: E402

CODE_REF = "packing-" + hashlib.sha256((HERE / "packing.py").read_bytes()).hexdigest()[:12]
CONFIG_SCHEMA = {
    "type": "object", "required": ["init", "optimizer", "iters"], "additionalProperties": False,
    "properties": {
        "init": {"enum": ["grid", "hex", "random", "lattice_perturb"]},
        "optimizer": {"enum": ["slsqp", "penalty"]},
        "iters": {"type": "integer", "minimum": 1, "maximum": 5000},
        "perturb": {"type": "number", "minimum": 0, "maximum": 0.2},
    },
}
GOAL = "Maximize the sum of radii of 26 non-overlapping circles in the unit square."
SPACE = {"init": ["grid", "hex", "random", "lattice_perturb"], "optimizer": ["slsqp", "penalty"],
         "iters": [10, 30, 100, 300, 1000], "perturb": [0.0, 0.01, 0.02, 0.05]}


def evaluate(cfg: dict[str, Any], seed: int) -> dict[str, Any]:
    from evaluator import score_circles
    from packing import solve

    t = time.monotonic()
    try:
        circles, info = solve(cfg, seed)
    except Exception as e:  # noqa: BLE001
        return {"run_status": "error", "metrics": {}, "notes": f"solver crashed: {e}"[:300],
                "duration_ms": int((time.monotonic() - t) * 1000)}
    res = score_circles(circles)
    ms = int((time.monotonic() - t) * 1000)
    if "invalid" in res:
        return {"run_status": "invalid", "metrics": {}, "notes": res["invalid"], "duration_ms": ms}
    notes = None
    if info.get("converged") is False:
        notes = f"SLSQP did not converge: {info.get('message')}"
    return {"run_status": "ok", "metrics": {"sum_radii": round(res["sum_radii"], 10)}, "notes": notes,
            "duration_ms": ms}


def sample(space: dict[str, list[Any]], rng: random.Random) -> dict[str, Any]:
    return {k: rng.choice(v) for k, v in space.items()}


# ---------------------------------------------------------------------------- worker

def worker_main(i: int, project: str, deadline: float, careless: float, log_path: str | None) -> None:
    lg = Ledger.open(project=project, agent=f"worker-{i}", role="worker")
    rng = random.Random(1000 + i)
    evals_log = open(log_path, "a") if log_path else None  # noqa: SIM115
    while time.monotonic() < deadline:
        task = lg.next_task(lease_seconds=60)
        if task is None:
            time.sleep(0.5)
            continue
        lg.brief(task.id, budget_tokens=1500)
        spec = task.spec
        n_done, ids = 0, []
        try:
            if "replicate" in spec:
                cfg = spec["replicate"]
                for seed in spec.get("seeds", []):
                    if lg.check_tried(cfg, CODE_REF).exact and any(
                            e.seed == seed and e.run_status == "ok" for e in lg.check_tried(cfg, CODE_REF).exact):
                        continue
                    ids.append(run_one(lg, cfg, seed, task.id, evals_log))
                    n_done += 1
            else:
                space = spec.get("config_space", SPACE)
                for _ in range(int(spec.get("evaluations", 4))):
                    if time.monotonic() > deadline:
                        break
                    for _attempt in range(12):
                        cfg, seed = sample(space, rng), rng.choice([0, 1, 2])
                        tried = lg.check_tried(cfg, CODE_REF)
                        same = [e for e in tried.exact if e.seed == seed]
                        if not same or rng.random() < careless:
                            break
                    else:
                        break  # this corner of the space is exhausted
                    ids.append(run_one(lg, cfg, seed, task.id, evals_log))
                    n_done += 1
                    lg.heartbeat(task.id, 60)
            summary = f"{n_done} evaluations; records: {', '.join(x for x in ids if x)}"[:900]
            lg.complete_task(task.id, "done", summary)
        except LeaseLost:
            continue
        except AssayError as e:
            print(f"worker-{i}: {e}", file=sys.stderr)
    if evals_log:
        evals_log.close()


def run_one(lg: Ledger, cfg: dict[str, Any], seed: int, task_id: Any, evals_log: Any) -> str:
    res = evaluate(cfg, seed)
    if evals_log is not None:
        evals_log.write(json.dumps({"t": time.time(), "agent": lg.agent, "config": cfg, "code_ref": CODE_REF,
                                    "seed": seed, **res}) + "\n")
        evals_log.flush()
    try:
        r = lg.record_experiment(cfg, res["metrics"], run_status=res["run_status"], seed=seed, code_ref=CODE_REF,
                                 notes=res["notes"], task_id=task_id, duration_ms=res["duration_ms"])
    except LeaseLost:
        r = lg.record_experiment(cfg, res["metrics"], run_status=res["run_status"], seed=seed, code_ref=CODE_REF,
                                 notes=res["notes"], duration_ms=res["duration_ms"])
    if r.id is None:
        return ""
    eid = f"e:{str(r.id)[:8]}"
    if r.is_new_best:
        lg.propose_claim("observation", f"{cfg['init']} init with {cfg['optimizer']} reaches "
                         f"{res['metrics']['sum_radii']:.4f}", evidence=[(r.id, "supports")],
                         tags=[cfg["init"], cfg["optimizer"]])
    if res["run_status"] == "ok" and cfg["optimizer"] == "penalty" and r.outcome == "worse":
        lg.propose_claim("hypothesis", "The penalty optimizer underperforms SLSQP for n=26",
                         evidence=[(r.id, "supports")], tags=["optimizer"])
    return eid


# ---------------------------------------------------------------------------- lead

def lead_round(lead: Ledger, rng: random.Random, n_workers: int) -> None:
    st = lead.status()
    live = st["tasks"].get("claimable", 0) + st["tasks"].get("claimed", 0)
    if live < n_workers:
        best = lead.sql("SELECT config FROM v_best")["rows"]
        tasks = []
        for init in SPACE["init"]:
            sp = dict(SPACE, init=[init])
            tasks.append({"title": f"Explore {init} init", "tags": ["explore", init],
                          "spec": {"config_space": sp, "evaluations": 4}})
        if best:
            b = best[0]["config"]
            fine = {"init": [b["init"]], "optimizer": [b["optimizer"]], "iters": [b["iters"], 2 * b["iters"]],
                    "perturb": sorted({round(rng.uniform(0, 0.06), 3) for _ in range(6)})}
            tasks.append({"title": f"Refine around best ({b['init']}/{b['optimizer']})", "priority": 5,
                          "tags": ["refine", b["init"]], "spec": {"config_space": fine, "evaluations": 6}})
        lead.create_tasks(tasks[: max(1, n_workers - live)])
    verify_best(lead)


def verify_best(lead: Ledger) -> None:
    top = lead.sql_internal(
        "SELECT dedup_key, config FROM assay.v_tried WHERE project_id=%s AND code_ref=%s"
        " AND best_value IS NOT NULL ORDER BY best_value DESC, dedup_key LIMIT 1",
        (lead.project_id, CODE_REF))
    if not top:
        return
    t = top[0]
    exps = lead.sql_internal(
        "SELECT id, seed, author, primary_value FROM assay.experiments_valid WHERE project_id=%s AND dedup_key=%s"
        " AND run_status='ok' AND seed IS NOT NULL AND primary_value IS NOT NULL ORDER BY created_at, id",
        (lead.project_id, t["dedup_key"]))
    seeds = sorted({e["seed"] for e in exps})
    authors = {e["author"] for e in exps}
    if len(seeds) < 3 or len(authors) < 2:
        open_rep = lead.sql_internal(
            "SELECT 1 FROM assay.tasks WHERE project_id=%s AND status IN ('open','claimed') AND spec->'replicate' = %s",
            (lead.project_id, json.dumps(t["config"])))
        if not open_rep:
            new = [s for s in range(3, 30) if s not in seeds][: max(1, 3 - len(seeds) + 1)]
            lead.create_tasks([{"title": "Replicate the current best with new seeds", "priority": 10,
                                "tags": ["replicate"], "spec": {"replicate": t["config"], "seeds": new}}])
        return
    lo = min(e["primary_value"] for e in exps)
    from assay.ledger import config_line

    tested = ", ".join(map(str, seeds))
    stmt = f"{config_line(t['config'])}: minimum sum_radii {lo!r} across {len(seeds)} tested seeds ({tested})"
    c = lead.propose_claim("conclusion", stmt, evidence=[(e["id"], "supports") for e in exps], tags=["best"])
    status = lead.claim_status(c.id)
    ev = [e["id"] for e in exps]
    if status == "proposed":
        lead.decide(c.id, "supported", "metric", f"{len(exps)} runs, observed minimum {lo!r}", ev)
        status = "supported"
    if status == "supported":
        lead.decide(c.id, "verified", "rerun",
                    f"Observed minimum {lo!r} across {len(seeds)} tested seeds by {len(authors)} agents", ev,
                    oracle_detail={"seeds": seeds, "authors": sorted(authors)})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--project", default="circle-packing-26")
    ap.add_argument("--careless", type=float, default=0.15, help="chance a worker re-runs a tried config anyway")
    ap.add_argument("--evals-log", default=None, help="append every evaluation to this JSONL file")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    Ledger.init_project(args.project, title="Circle packing n=26", goal=GOAL, metric="sum_radii", direction="max",
                        epsilon=1e-6, config_schema=CONFIG_SCHEMA)
    lead = Ledger.open(project=args.project, agent="lead")
    rng = random.Random(args.seed)
    # monotonic: CLOCK_MONOTONIC is shared by all processes and immune to wall-clock jumps
    deadline = time.monotonic() + args.minutes * 60
    lead_round(lead, rng, args.workers)
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=worker_main, args=(i + 1, args.project, deadline, args.careless, args.evals_log))
             for i in range(args.workers)]
    for p in procs:
        p.start()
    print(f"{args.workers} workers on project {args.project} for {args.minutes} min (code_ref {CODE_REF})")
    while time.monotonic() < deadline:
        time.sleep(3)
        lead_round(lead, rng, args.workers)
        s = lead.status()
        best = s["best"]["value"] if s["best"] else None
        print(f"  best={best}  exps={s['experiments']['total']}  dup_rejected={s['dedup']['rejected']}"
              f"  repeat_checks={s['dedup']['avoided']}  verified={s['claims'].get('verified', 0)}", flush=True)
    for p in procs:
        p.join(timeout=90)
    verify_best(lead)
    s = lead.status()
    print(json.dumps(s, indent=2, default=str))


if __name__ == "__main__":
    main()
