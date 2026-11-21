"""Run a swarm of scripted agents on the CPU simulator and check the §12 acceptance conditions.

    python examples/autoresearch-sim/run_sim_swarm.py --agents 4 --minutes 5
    python examples/autoresearch-sim/run_sim_swarm.py --agents 3 --minutes 1 --sleep 0.2   # CI

Uses BENCHBOOK_DATABASE_URL when set, else the local embedded Postgres. Each agent gets its own git worktree,
like one GPU each on a single machine (§11 S1). Exits 1 when any condition fails. This is a pipeline check,
not evidence about real autoresearch.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SIM_FILES = ["prepare.py", "train.py"]


def sh(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({r.returncode}): {r.stdout}{r.stderr}")
    return r.stdout


def setup(root: Path, project: str, agents: int, env: dict[str, str]) -> list[Path]:
    main = root / "main"
    main.mkdir(parents=True)
    for f in SIM_FILES:
        shutil.copy(HERE / f, main / f)
    (main / ".gitignore").write_text("__pycache__/\nresults.tsv\n", encoding="utf-8")
    sh(["git", "init", "-q", "-b", "master"], main)
    sh(["git", "-c", "user.name=sim", "-c", "user.email=sim@example.invalid", "add", "-A"], main)
    sh(["git", "-c", "user.name=sim", "-c", "user.email=sim@example.invalid", "commit", "-qm", "simulator"], main)
    sh([sys.executable, "-m", "benchbook", "ar", "init", "--project", project], main, env)
    trees = []
    for i in range(agents):
        wt = root / f"gpu{i}"
        sh(["git", "worktree", "add", "-q", "-b", f"autoresearch/sim-gpu{i}", str(wt)], main)
        shutil.copy(main / "program.benchbook.md", wt / "program.benchbook.md")
        for k, v in (("user.name", f"gpu{i}"), ("user.email", f"gpu{i}@example.invalid")):
            sh(["git", "config", k, v], wt)
        trees.append(wt)
    return trees


def check(project: str, trees: list[Path], invocations: Path) -> tuple[dict[str, Any], list[str]]:
    from benchbook import Ledger

    lg = Ledger.open(project=project, agent="human")
    q = lg.sql_internal
    pid = lg.project_id
    bad: list[str] = []
    dups = q("SELECT config->>'partition' AS p, code_ref, coalesce(seed,0) AS seed, count(*) AS n"
             " FROM benchbook.experiments_valid WHERE project_id=%s AND run_status='ok'"
             " GROUP BY 1,2,3 HAVING count(*)>1",
             (pid,))
    if dups:
        bad.append(f"{len(dups)} code/replication-number pairs ran ok more than once")
    started = {line.strip() for line in invocations.read_text().splitlines() if line.strip()} \
        if invocations.exists() else set()
    recorded = {str(int(r["rid"])) for r in q(
        "SELECT (metrics->>'sim_run_id')::numeric AS rid FROM benchbook.experiments WHERE project_id=%s"
        " AND metrics ? 'sim_run_id'", (pid,))}
    lost = started - recorded
    if lost:
        bad.append(f"{len(lost)} runs started by benchbook ar but not recorded")
    results = q("SELECT s.id, s.status, c.payload FROM benchbook.v_claim_status s JOIN benchbook.claims c ON c.id=s.id"
                " WHERE s.project_id=%s AND s.kind='result'", (pid,))
    for r in results:
        if r["status"] != "verified":
            continue
        pl = r["payload"]
        runs = q("SELECT * FROM benchbook.experiments_valid WHERE id = ANY(%s::uuid[])", (pl["experiment_ids"],))
        ok = [x for x in runs if x["run_status"] == "ok"]
        mean = sum(x["primary_value"] for x in ok) / len(ok) if ok else None
        rule = (len(ok) >= 3 and len({x["seed"] for x in ok}) >= 3 and len({x["author"] for x in ok}) >= 2
                and len({x["dedup_key"] for x in ok}) == 1 and mean is not None
                and (pl.get("baseline") is None or mean < pl["baseline"] - pl["epsilon"]))
        if not rule:
            bad.append(f"verified result c:{str(r['id'])[:8]} breaks the replication rule (T5)")
    lucky = q("SELECT e.id, e.primary_value, s.status, c.payload FROM benchbook.experiments_valid e"
              " LEFT JOIN benchbook.links l ON l.src_id=e.id AND l.relation='supports'"
              " LEFT JOIN benchbook.claims c ON c.id=l.dst_id AND c.kind='result'"
              " LEFT JOIN benchbook.v_claim_status s ON s.id=c.id"
              " WHERE e.project_id=%s AND coalesce(e.seed,0)=0 AND e.outcome='improved'"
              " AND (e.metrics->>'lucky')::numeric = 1", (pid,))
    lucky_seen = [x for x in lucky if x["status"] is not None]
    for x in lucky_seen:
        if x["status"] == "verified":
            pl = x["payload"]
            others = q("SELECT primary_value FROM benchbook.experiments_valid WHERE id = ANY(%s::uuid[]) AND id <> %s"
                       " AND run_status='ok'", (pl["experiment_ids"], x["id"]))
            m = sum(o["primary_value"] for o in others) / len(others) if others else None
            if pl.get("baseline") is not None and (m is None or m >= pl["baseline"] - pl["epsilon"]):
                bad.append(f"lucky run e:{str(x['id'])[:8]} was verified on the strength of the lucky run")
    lg.refresh()
    parts = [r["p"] for r in q("SELECT DISTINCT config->>'partition' AS p FROM benchbook.experiments"
                               " WHERE project_id=%s",
                               (pid,))]
    cal = lg.project.settings.get("partitions") or {}
    for p in parts:
        if p not in cal:
            bad.append(f"partition {p} was never calibrated")
    tsv_rows = 0
    for wt in trees:
        f = wt / "results.tsv"
        if not f.exists():
            continue
        with open(f, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh, delimiter="\t"))
        if rows[0] != ["commit", "val_bpb", "memory_gb", "status", "description"]:
            bad.append(f"{f}: header {rows[0]}")
        for row in rows[1:]:
            tsv_rows += 1
            try:
                assert len(row) == 5 and row[3] in ("keep", "discard", "crash")
                float(row[1]), float(row[2])
                if row[3] == "crash":
                    assert row[1] == "0.000000" and row[2] == "0.0"
            except (AssertionError, ValueError):
                bad.append(f"{f}: malformed row {row}")
        pending = wt / ".benchbook" / "ar_pending.jsonl"
        if pending.exists() and pending.read_text().strip():
            bad.append(f"{pending}: runs still waiting to be recorded")
    counts = q("SELECT count(*) AS runs, count(*) FILTER (WHERE coalesce(seed,0)>0) AS replications,"
               " count(*) FILTER (WHERE run_status<>'ok') AS failed,"
               " coalesce(sum(duration_ms) FILTER (WHERE coalesce(seed,0)>0),0)::float8"
               "   / nullif(sum(duration_ms),0) AS replication_share"
               " FROM benchbook.experiments WHERE project_id=%s", (pid,))[0]
    # refusals before running only: not 'rejected' hits, not refusals the agent overrode with --retry-failed
    blocked = q("SELECT count(*) AS n FROM benchbook.dedup_hits h WHERE h.project_id=%s AND h.kind='avoided'"
                " AND NOT EXISTS (SELECT 1 FROM benchbook.experiments x WHERE x.project_id=h.project_id"
                "   AND x.dedup_key=h.dedup_key AND x.author=h.agent_id AND coalesce(x.seed,0)=0"
                "   AND x.created_at >= h.created_at)", (pid,))[0]["n"]
    verdicts: dict[str, int] = {}
    for r in results:
        verdicts[r["status"]] = verdicts.get(r["status"], 0) + 1
    report = {"project": pid, "runs": counts["runs"], "replications": counts["replications"],
              "failed": counts["failed"], "replication_gpu_share": counts["replication_share"],
              "duplicates_blocked": blocked, "results": verdicts, "lucky_candidates_judged": len(lucky_seen),
              "invocations": len(started), "unrecorded": len(lost), "results_tsv_rows": tsv_rows,
              "calibration": {p: cal.get(p) for p in parts}}
    return report, bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--sleep", type=float, default=None, help="simulated run seconds (default: prepare.TIME_BUDGET)")
    ap.add_argument("--lucky", type=float, default=0.02)
    ap.add_argument("--noise", type=float, default=0.002)
    ap.add_argument("--overlap", type=float, default=0.3)
    ap.add_argument("--project", default=None)
    ap.add_argument("--workdir", type=Path, default=None)
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    args = ap.parse_args(argv)
    project = args.project or f"sim-{uuid.uuid4().hex[:8]}"
    root = args.workdir or Path(tempfile.mkdtemp(prefix="benchbook-ar-sim-"))
    env = dict(os.environ, BENCHBOOK_PROJECT=project, BENCHBOOK_AR_HW="sim-cpu", AR_SIM_LUCKY=str(args.lucky),
               AR_SIM_NOISE=str(args.noise), AR_SIM_INVOCATIONS=str(root / "invocations.log"))
    env.pop("CUDA_VISIBLE_DEVICES", None)
    if args.sleep is not None:
        env["AR_SIM_SECONDS"] = str(args.sleep)
    t0 = time.monotonic()
    trees = setup(root, project, args.agents, env)
    procs = []
    for i, wt in enumerate(trees):
        e = dict(env, BENCHBOOK_AGENT=f"gpu{i}")
        procs.append(subprocess.Popen(
            [sys.executable, str(HERE / "scripted_agent.py"), "--minutes", str(args.minutes), "--seed", str(i),
             "--overlap", str(args.overlap)], cwd=wt, env=e, stdout=open(root / f"gpu{i}.log", "w"),
            stderr=subprocess.STDOUT))
    codes = [p.wait() for p in procs]
    from benchbook import Ledger
    from benchbook.integrations.autoresearch.referee import tick

    tick(Ledger.open(project=project, agent="referee", role="orchestrator"))
    report, bad = check(project, trees, root / "invocations.log")
    if any(codes):
        bad.append(f"agent exit codes {codes}; see {root}/gpu*.log")
    report.update(agents=args.agents, minutes=args.minutes, wall_seconds=round(time.monotonic() - t0, 1),
                  workdir=str(root), violations=bad, label="pipeline check, not evidence")
    print(json.dumps(report, indent=2, default=str))
    if not args.keep and not bad and args.workdir is None:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
