"""Launch experiment harness (§13): arm A (independent upstream autoresearch) vs arm B (assay swarm).

Real mode prepares worktrees and prints the commands; nothing is started without --execute (cost rule):

    python -m bench.autoresearch.launch --upstream ~/autoresearch --gpus-a 0,1,2,3 --gpus-b 4,5,6,7 \
        --hours 8 --model claude-sonnet-4-5-20250929 --max-budget-usd 40 [--execute]

--sim runs the whole pipeline on the CPU simulator with scripted agents (arm A: sim_agent_a.py behind the
wrap_train.sh shim; arm B: examples/autoresearch-sim/scripted_agent.py), then post_replicate and report.
Its output is a pipeline check, not evidence.

    python -m bench.autoresearch.launch --sim --agents 2 --minutes 1
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SIM = ROOT / "examples" / "autoresearch-sim"
PROMPT_A = ("Read program.md and start. Setup is done for you: the run tag is {tag}, the branch {branch} already "
            "exists and is checked out, the data is prepared, and results.tsv does not exist yet. Do not ask for "
            "confirmation; go straight to the baseline run and then the experiment loop.")
PROMPT_B = ("Read program.assay.md and start. Setup is done for you: ASSAY_AGENT and ASSAY_PROJECT are set and the "
            "data is prepared. Do not ask for confirmation; go straight to the loop.")
TOOLS = "Bash,Read,Edit,Write,Glob,Grep"


def sh(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(map(shlex.quote, args))} failed ({r.returncode}): {r.stdout}{r.stderr}")
    return r.stdout.strip()


def make_sim_upstream(path: Path) -> Path:
    path.mkdir(parents=True)
    for f in ("prepare.py", "train.py"):
        shutil.copy(SIM / f, path / f)
    (path / "program.md").write_text("# simulated upstream program.md (sim mode)\n", encoding="utf-8")
    (path / ".gitignore").write_text("results.tsv\nrun.log\n__pycache__/\n", encoding="utf-8")
    sh(["git", "init", "-q", "-b", "master"], path)
    sh(["git", "-c", "user.name=sim", "-c", "user.email=sim@example.invalid", "add", "-A"], path)
    sh(["git", "-c", "user.name=sim", "-c", "user.email=sim@example.invalid", "commit", "-qm", "upstream"], path)
    return path


def worktree(upstream: Path, dest: Path, branch: str) -> Path:
    sh(["git", "-C", str(upstream), "worktree", "add", "-q", "-b", branch, str(dest)])
    return dest


def install_shim(out: Path) -> Path:
    shim = out / "shim"
    shim.mkdir(exist_ok=True)
    dst = shim / "uv"
    shutil.copy(HERE / "wrap_train.sh", dst)
    dst.chmod(0o755)
    return shim


def prepare(args: Any, out: Path) -> dict[str, Any]:
    tag = args.tag or datetime.now(UTC).strftime("ar%m%d%H%M")
    upstream = make_sim_upstream(out / "upstream") if args.sim else Path(args.upstream).expanduser().resolve()
    if not (upstream / "train.py").is_file() or not (upstream / "prepare.py").is_file():
        raise SystemExit(f"{upstream} is not an autoresearch checkout")
    gpus_a = list(range(args.agents)) if args.sim else [int(x) for x in args.gpus_a.split(",") if x != ""]
    gpus_b = list(range(args.agents, 2 * args.agents)) if args.sim else [int(x) for x in args.gpus_b.split(",")
                                                                          if x != ""]
    if len(gpus_a) != len(gpus_b) or not gpus_a:
        raise SystemExit("arms need the same, non-zero number of GPUs (--gpus-a / --gpus-b)")
    project = f"{tag}-b-{uuid.uuid4().hex[:6]}"
    shim = install_shim(out)
    real_uv = shutil.which("uv") or "uv"
    base_env = {k: v for k, v in os.environ.items() if k not in ("ASSAY_PROJECT", "ASSAY_AGENT")}
    agents: list[dict[str, Any]] = []
    for g in gpus_a:
        branch = f"autoresearch/{tag}-gpu{g}"
        wt = worktree(upstream, out / "A" / f"gpu{g}", branch)
        env = dict(base_env, CUDA_VISIBLE_DEVICES=str(g), PATH=f"{shim}{os.pathsep}{base_env.get('PATH', '')}",
                   AR_BENCH_LOG=str(out / "A" / "runs.jsonl"), AR_BENCH_AGENT=f"gpu{g}", AR_BENCH_REAL=real_uv)
        if args.sim:
            env.update(AR_BENCH_REAL_CMD=f"{sys.executable} train.py", AR_SIM_SECONDS=str(args.sleep))
            cmd = [sys.executable, str(HERE / "sim_agent_a.py"), "--minutes", str(args.minutes), "--seed", str(g)]
        else:
            cmd = claude_cmd(args, PROMPT_A.format(tag=f"{tag}-gpu{g}", branch=branch))
        agents.append({"arm": "A", "gpu": g, "cwd": str(wt), "cmd": cmd, "env": env})
    b_trees = []
    for g in gpus_b:
        wt = worktree(upstream, out / "B" / f"gpu{g}", f"autoresearch/{tag}-b-gpu{g}")
        b_trees.append(wt)
    init_env = dict(base_env, ASSAY_AR_HW="sim-cpu") if args.sim else dict(base_env)
    sh([sys.executable, "-m", "assay", "ar", "init", "--project", project], b_trees[0], init_env)
    for wt in b_trees[1:]:
        shutil.copy(b_trees[0] / "program.assay.md", wt / "program.assay.md")
    for g, wt in zip(gpus_b, b_trees, strict=True):
        env = dict(base_env, CUDA_VISIBLE_DEVICES=str(g), ASSAY_AGENT=f"gpu{g}", ASSAY_PROJECT=project)
        if args.sim:
            env.update(ASSAY_AR_HW="sim-cpu", AR_SIM_SECONDS=str(args.sleep))
            cmd = [sys.executable, str(SIM / "scripted_agent.py"), "--minutes", str(args.minutes), "--seed", str(g),
                   "--drain-seconds", "0"]
        else:
            cmd = claude_cmd(args, PROMPT_B)
        agents.append({"arm": "B", "gpu": g, "cwd": str(wt), "cmd": cmd, "env": env})
    conditions = {
        "label": "pipeline check, not evidence" if args.sim else "real run",
        "tag": tag, "project_b": project, "upstream": str(upstream),
        "upstream_commit": sh(["git", "-C", str(upstream), "rev-parse", "HEAD"]),
        "gpus_a": gpus_a, "gpus_b": gpus_b, "hours": args.hours, "minutes": args.minutes if args.sim else None,
        "model": None if args.sim else args.model, "max_budget_usd_per_agent": None if args.sim else
        args.max_budget_usd, "claude_version": None if args.sim else _claude_version(),
        "assay_commit": sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "database": "ASSAY_DATABASE_URL" if os.environ.get("ASSAY_DATABASE_URL") else "embedded",
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agents": [{k: a[k] for k in ("arm", "gpu", "cwd", "cmd")} for a in agents],
    }
    (out / "conditions.json").write_text(json.dumps(conditions, indent=2), encoding="utf-8")
    lines = ["#!/usr/bin/env bash", "# generated by bench/autoresearch/launch.py; one line per agent", "set -e"]
    for a in agents:
        keys = ["CUDA_VISIBLE_DEVICES", "PATH", "AR_BENCH_LOG", "AR_BENCH_AGENT", "AR_BENCH_REAL", "ASSAY_AGENT",
                "ASSAY_PROJECT", "ASSAY_AR_HW", "AR_SIM_SECONDS", "AR_BENCH_REAL_CMD"]
        envs = " ".join(f"{k}={shlex.quote(a['env'][k])}" for k in keys if k in a["env"])
        log = shlex.quote(str(out / a["arm"] / f"agent-gpu{a['gpu']}.jsonl"))
        lines.append(f"(cd {shlex.quote(a['cwd'])} && {envs} {shlex.join(a['cmd'])} > {log} 2>&1) &")
    lines.append("wait")
    (out / "commands.sh").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"agents": agents, "conditions": conditions}


def claude_cmd(args: Any, prompt: str) -> list[str]:
    return ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--model", args.model, "--max-budget-usd", str(args.max_budget_usd), "--allowedTools", TOOLS]


def _claude_version() -> str | None:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def execute(prep: dict[str, Any], out: Path, seconds: float) -> list[int | None]:
    procs = []
    for a in prep["agents"]:
        log = open(out / a["arm"] / f"agent-gpu{a['gpu']}.jsonl", "wb")
        procs.append(subprocess.Popen(a["cmd"], cwd=a["cwd"], env=a["env"], stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and any(p.poll() is None for p in procs):
        time.sleep(1)
    for p in procs:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for p in procs:
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    ended = datetime.now(UTC).isoformat(timespec="seconds")
    cond = json.loads((out / "conditions.json").read_text())
    cond["ended_at"] = ended
    cond["exit_codes"] = [p.returncode for p in procs]
    (out / "conditions.json").write_text(json.dumps(cond, indent=2), encoding="utf-8")
    return [p.returncode for p in procs]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="CPU simulator and scripted agents (pipeline check)")
    ap.add_argument("--upstream", help="an autoresearch checkout (upstream or a fork), data already prepared")
    ap.add_argument("--gpus-a", default="0,1,2,3")
    ap.add_argument("--gpus-b", default="4,5,6,7")
    ap.add_argument("--agents", type=int, default=2, help="--sim: agents per arm")
    ap.add_argument("--hours", type=float, default=8.0, help="wall clock per arm (real)")
    ap.add_argument("--minutes", type=float, default=1.0, help="--sim: minutes per arm")
    ap.add_argument("--sleep", type=float, default=0.2, help="--sim: simulated run seconds")
    ap.add_argument("--model", default=None, help="a dated model identifier (real)")
    ap.add_argument("--max-budget-usd", type=float, default=None, help="Claude dollar cap per agent (real)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--execute", action="store_true", help="real: start the agents (costs GPU time and API money)")
    args = ap.parse_args(argv)
    if not args.sim and (not args.upstream or not args.model or args.max_budget_usd is None):
        ap.error("real mode needs --upstream, --model and --max-budget-usd (see MANUAL_QA.md)")
    out = (args.out or ROOT / "bench" / "out" / f"autoresearch-{datetime.now(UTC):%Y%m%d-%H%M%S}").resolve()
    out.mkdir(parents=True, exist_ok=False)
    for arm in ("A", "B"):
        (out / arm).mkdir()
    prep = prepare(args, out)
    if args.sim:
        execute(prep, out, args.minutes * 60 + 120)
        from . import post_replicate, report

        post_replicate.main([str(out), "--sim"])
        report.main([str(out)])
        print(f"pipeline check done: {out / 'report.md'}")
        return 0
    print(f"prepared {out}\n  conditions: {out / 'conditions.json'}\n  commands:   {out / 'commands.sh'}")
    if not args.execute:
        print("Nothing was started. Review commands.sh, commit PREREGISTRATION.md, then re-run with --execute.")
        return 0
    codes = execute(prep, out, args.hours * 3600)
    print(f"agents stopped (exit codes {codes}). Next: python -m bench.autoresearch.post_replicate {out} --gpu <n>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
