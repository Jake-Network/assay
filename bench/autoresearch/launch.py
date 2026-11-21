"""Launch experiment harness (§13): arm A (independent upstream autoresearch) vs arm B (benchbook swarm).

Real mode prepares worktrees and prints the commands; nothing is started without --execute (cost rule):

    python -m bench.autoresearch.launch --upstream ~/autoresearch --gpus-a 0,1,2,3 --gpus-b 4,5,6,7 \
        --hours 8 --model claude-sonnet-4-5-20250929 --max-budget-usd 40 [--execute]

--sim runs the whole pipeline on the CPU simulator with scripted agents (arm A: sim_agent_a.py; arm B:
examples/autoresearch-sim/scripted_agent.py; both behind the same wrap_train.sh shim), then post_replicate
and report. Repeat with --repetition k --assign-seed k for each pre-registered repetition.
Its output is a pipeline check, not evidence.

    python -m bench.autoresearch.launch --sim --agents 2 --minutes 1
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from benchbook import config as benchbook_config
from benchbook.integrations.autoresearch.code import code_hash

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SIM = ROOT / "examples" / "autoresearch-sim"
PROMPT_A = ("Read program.md and start. Setup is done for you: the run tag is {tag}, the branch {branch} already "
            "exists and is checked out, the data is prepared, and results.tsv does not exist yet. Do not ask for "
            "confirmation; go straight to the baseline run and then the experiment loop.")
PROMPT_B = ("Read program.benchbook.md and start. Setup is done for you: BENCHBOOK_AGENT and "
            "BENCHBOOK_PROJECT are set and the data is prepared. Do not ask for confirmation; go straight to the loop.")
TRAINING_PROMPT = " For this experiment, launch training through the supplied PATH with `uv run train.py`."
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
    dated = dict(os.environ, GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
    sh(["git", "-c", "user.name=sim", "-c", "user.email=sim@example.invalid", "commit", "-qm", "upstream"], path, dated)
    return path


def worktree(upstream: Path, dest: Path, branch: str) -> Path:
    sh(["git", "-C", str(upstream), "worktree", "add", "-q", "-b", branch, str(dest)])
    return dest


def install_shim(out: Path) -> Path:
    shim = out / "shim"
    shim.mkdir(exist_ok=True)
    for name in ("uv", "python", "python3"):
        if name != "uv" and shutil.which(name) is None:
            continue
        dst = shim / name
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
    if len(set(gpus_a + gpus_b)) != len(gpus_a) + len(gpus_b):
        raise SystemExit("each GPU must appear exactly once across the two arms")
    # B6: which GPU group gets which arm is a coin flip (NUMA / PCIe groups may differ), recorded for the report
    swapped = args.assign_seed is not None and random.Random(args.assign_seed).random() < 0.5
    if swapped:
        gpus_a, gpus_b = gpus_b, gpus_a
    project = f"{tag}-b-{uuid.uuid4().hex[:6]}"
    shim = install_shim(out)
    real_uv = shutil.which("uv")
    if real_uv is None and not args.sim:
        raise SystemExit("uv must be on PATH to collect training runs")
    # neither arm inherits the launching shell's project or agent, under the current or the pre-rename variable name
    inherited = {prefix + name for prefix in (benchbook_config.ENV_PREFIX, benchbook_config.LEGACY_ENV_PREFIX)
                 for name in ("PROJECT", "AGENT")}
    base_env = {k: v for k, v in os.environ.items() if k not in inherited}

    def shim_env(arm: str, g: int) -> dict[str, str]:
        """B1: the same uv shim counts runs in both arms."""
        env = {"PATH": f"{shim}{os.pathsep}{base_env.get('PATH', '')}", "AR_BENCH_LOG": str(out / arm / "runs.jsonl"),
               "AR_BENCH_AGENT": f"gpu{g}", "AR_BENCH_REAL": real_uv or "false", "AR_BENCH_PY": sys.executable,
               "AR_BENCH_HELPER": str(HERE / "shim_log.py")}
        for name in ("python", "python3"):
            if binary := shutil.which(name):
                env[f"AR_BENCH_REAL_{name.upper()}"] = binary
        if args.sim:
            env.update(AR_BENCH_REAL_CMD=f"{sys.executable} train.py", AR_SIM_SECONDS=str(args.sleep))
        return env

    agents: list[dict[str, Any]] = []
    for g in gpus_a:
        branch = f"autoresearch/{tag}-gpu{g}"
        wt = worktree(upstream, out / "A" / f"gpu{g}", branch)
        env = dict(base_env, CUDA_VISIBLE_DEVICES=str(g), **shim_env("A", g))
        if args.sim:
            cmd = [sys.executable, str(HERE / "sim_agent_a.py"), "--minutes", str(args.minutes), "--seed", str(g)]
        else:
            cmd = claude_cmd(args, PROMPT_A.format(tag=f"{tag}-gpu{g}", branch=branch) + TRAINING_PROMPT)
        agents.append({"arm": "A", "gpu": g, "cwd": str(wt), "cmd": cmd, "env": env})
    b_trees = []
    for g in gpus_b:
        wt = worktree(upstream, out / "B" / f"gpu{g}", f"autoresearch/{tag}-b-gpu{g}")
        b_trees.append(wt)
    init_env = dict(base_env, BENCHBOOK_AR_HW="sim-cpu") if args.sim else dict(base_env)
    sh([sys.executable, "-m", "benchbook", "ar", "init", "--project", project], b_trees[0], init_env)
    for wt in b_trees[1:]:
        shutil.copy(b_trees[0] / "program.benchbook.md", wt / "program.benchbook.md")
    for g, wt in zip(gpus_b, b_trees, strict=True):
        env = dict(base_env, CUDA_VISIBLE_DEVICES=str(g), BENCHBOOK_AGENT=f"gpu{g}", BENCHBOOK_PROJECT=project,
                   **shim_env("B", g))
        if args.sim:
            env.update(BENCHBOOK_AR_HW="sim-cpu")
            cmd = [sys.executable, str(SIM / "scripted_agent.py"), "--minutes", str(args.minutes), "--seed", str(g),
                   "--drain-seconds", "0", "--cmd", "uv run train.py"]
        else:
            cmd = claude_cmd(args, PROMPT_B + TRAINING_PROMPT)
        agents.append({"arm": "B", "gpu": g, "cwd": str(wt), "cmd": cmd, "env": env})
    conditions = {
        "label": "pipeline check, not evidence" if args.sim else "real run",
        "tag": tag, "project_b": project, "upstream": str(upstream),
        "upstream_commit": sh(["git", "-C", str(upstream), "rev-parse", "HEAD"]),
        "gpus_a": gpus_a, "gpus_b": gpus_b, "gpu_assignment": {"seed": args.assign_seed, "swapped": swapped},
        "repetition": args.repetition, "max_restarts": args.max_restarts,
        "baseline_code_hash": code_hash((upstream / "train.py").read_bytes()),
        "hours": args.hours, "minutes": args.minutes if args.sim else None,
        "gpu_hours_per_arm": None if args.sim else getattr(args, "gpu_hours_per_arm", None),
        "sim_run_seconds": args.sleep if args.sim else None,
        "model": None if args.sim else args.model, "max_budget_usd_per_agent": None if args.sim else
        args.max_budget_usd, "claude_version": None if args.sim else _claude_version(),
        "benchbook_commit": sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "database": "BENCHBOOK_DATABASE_URL" if benchbook_config.env("DATABASE_URL") else "embedded",
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agents": [{k: a[k] for k in ("arm", "gpu", "cwd", "cmd")} for a in agents],
    }
    (out / "conditions.json").write_text(json.dumps(conditions, indent=2), encoding="utf-8")
    lines = ["#!/usr/bin/env bash", "# generated by bench/autoresearch/launch.py; one line per agent"]
    if not args.sim:
        lines.append("# Preview only. Use launch --execute to supervise both arms and enforce budgets.")
    for a in agents:
        keys = ["CUDA_VISIBLE_DEVICES", "PATH", "AR_BENCH_LOG", "AR_BENCH_AGENT", "AR_BENCH_REAL", "AR_BENCH_PY",
                "AR_BENCH_REAL_PYTHON", "AR_BENCH_REAL_PYTHON3", "AR_BENCH_HELPER", "BENCHBOOK_AGENT",
                "BENCHBOOK_PROJECT", "BENCHBOOK_AR_HW", "AR_SIM_SECONDS", "AR_BENCH_REAL_CMD"]
        envs = " ".join(f"{k}={shlex.quote(a['env'][k])}" for k in keys if k in a["env"])
        log = shlex.quote(str(out / a["arm"] / f"agent-gpu{a['gpu']}.jsonl"))
        prefix = "" if args.sim else "# "
        lines.append(f"{prefix}(cd {shlex.quote(a['cwd'])} && {envs} {shlex.join(a['cmd'])} > {log} 2>&1) &")
    if args.sim:
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


@dataclass
class Session:
    process: subprocess.Popen[bytes]
    log: Path
    offset: int
    budget: Decimal | None
    result_count: int = 0
    cost: Decimal | None = None
    capped: bool = False

    def read_result(self) -> None:
        with self.log.open("rb") as fh:
            fh.seek(self.offset)
            while line := fh.readline():
                if not line.endswith(b"\n") and self.process.poll() is None:
                    break  # the writer has not flushed a full event yet
                self.offset = fh.tell()
                try:
                    event = json.loads(line, parse_float=Decimal)
                except (ValueError, UnicodeError):
                    continue  # stderr may share this log; the result receipt is required below
                if isinstance(event, dict) and event.get("type") == "result":
                    self.result_count += 1
                    self.capped |= event.get("subtype") == "error_max_budget_usd"
                    self.cost = result_cost(event.get("total_cost_usd"))

    @property
    def accounted(self) -> bool:
        return self.result_count == 1 and self.cost is not None


def result_cost(value: object) -> Decimal | None:
    if not isinstance(value, (int, float, Decimal, str)) or isinstance(value, bool):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _stop_agents(sessions: list[Session]) -> None:
    running = [s.process for s in sessions if s.process.poll() is None]
    for p in running:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 30
    for p in running:
        try:
            p.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()


def execute(prep: dict[str, Any], out: Path, seconds: float, max_restarts: int = 0,
            restart_margin: float = 60) -> list[int | None]:
    """Run both arms up to either cap. Restart only after a single valid cost receipt, with the remaining cap."""
    agents = prep["agents"]
    cond = json.loads((out / "conditions.json").read_text())
    cap = result_cost(cond.get("max_budget_usd_per_agent"))
    if cond.get("label") == "real run" and (cap is None or cap <= 0):
        raise ValueError("real launch requires a positive per-agent dollar cap")
    cutoff_reason = "wall_clock"
    if (gpu_hours := cond.get("gpu_hours_per_arm")) is not None:
        allocated_seconds = 3600 * gpu_hours / len(cond["gpus_a"])
        if allocated_seconds <= seconds:
            cutoff_reason, seconds = "gpu_hours", allocated_seconds

    def start(a: dict[str, Any], budget: Decimal | None) -> Session:
        cmd = list(a["cmd"])
        if budget is not None:
            i = cmd.index("--max-budget-usd")
            cmd[i + 1] = format(budget, "f")
        path = out / a["arm"] / f"agent-gpu{a['gpu']}.jsonl"
        with path.open("ab") as log:
            offset = log.tell()
            process = subprocess.Popen(cmd, cwd=a["cwd"], env=a["env"], stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        return Session(process, path, offset, budget)

    sessions: list[Session] = []
    prior: list[list[Session]] = [[] for _ in agents]
    restarts: list[list[dict[str, Any]]] = [[] for _ in agents]
    spent = [Decimal(0) for _ in agents]
    stop: dict[str, Any] = {"reason": cutoff_reason}
    t_start = time.monotonic()
    deadline = t_start + seconds
    try:
        for a in agents:
            sessions.append(start(a, cap))
        while (left := deadline - time.monotonic()) > 0:
            exited = [s.process.poll() is not None for s in sessions]
            if cap is not None:
                for i, session in enumerate(sessions):
                    session.read_result()
                    spent_session = session.cost
                    reason = None
                    if session.capped or (spent_session is not None and spent[i] + spent_session >= cap):
                        reason = "llm_budget"
                    elif session.result_count > 1 or (exited[i] and not session.accounted):
                        reason = "unaccounted_usage"
                    if reason:
                        stop = {"reason": reason, "arm": agents[i]["arm"], "gpu": agents[i]["gpu"]}
                        break
                if "arm" in stop:
                    break  # assess every arm before deciding whether any agent can restart
            for i, session in enumerate(sessions):
                if not exited[i] or len(restarts[i]) >= max_restarts or left <= restart_margin:
                    continue
                if session.cost is not None:
                    spent[i] += session.cost
                remaining = cap - spent[i] if cap is not None else None
                restart = {"after_s": round(time.monotonic() - t_start), "exit": session.process.returncode}
                if remaining is not None:
                    restart["remaining_budget_usd"] = str(remaining)
                replacement = start(agents[i], remaining)
                restarts[i].append(restart)
                prior[i].append(session)
                sessions[i] = replacement
                exited[i] = False
            if all(exited):
                stop = {"reason": "agents_exited"}
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    finally:
        active_seconds = time.monotonic() - t_start
        _stop_agents(sessions)
    if cap is not None:
        use = []
        for i, session in enumerate(sessions):
            session.read_result()
            attempts = [*prior[i], session]
            observed = sum((s.cost for s in attempts if s.accounted and s.cost is not None), Decimal(0))
            use.append({"arm": agents[i]["arm"], "gpu": agents[i]["gpu"], "observed_usd": str(observed),
                        "unaccounted_sessions": sum(not s.accounted for s in attempts)})
        cond["usage_accounting"] = use
    ended = datetime.now(UTC).isoformat(timespec="seconds")
    cond["ended_at"] = ended
    cond["stop"] = stop
    cond["active_seconds"] = active_seconds
    cond["exit_codes"] = [s.process.returncode for s in sessions]
    cond["restarts"] = {arm: sum(len(r) for a, r in zip(agents, restarts, strict=True) if a["arm"] == arm)
                        for arm in ("A", "B")}
    cond["restart_log"] = [{"arm": a["arm"], "gpu": a["gpu"], "restarts": r} for a, r in zip(agents, restarts,
                                                                                            strict=True)]
    (out / "conditions.json").write_text(json.dumps(cond, indent=2), encoding="utf-8")
    return cond["exit_codes"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sim", action="store_true", help="CPU simulator and scripted agents (pipeline check)")
    ap.add_argument("--upstream", help="an autoresearch checkout (upstream or a fork), data already prepared")
    ap.add_argument("--gpus-a", default="0,1,2,3")
    ap.add_argument("--gpus-b", default="4,5,6,7")
    ap.add_argument("--agents", type=int, default=2, help="--sim: agents per arm")
    ap.add_argument("--hours", type=float, default=8.0, help="wall clock per arm (real)")
    ap.add_argument("--gpu-hours-per-arm", type=float, default=None,
                    help="cap on allocated GPU hours per arm (real, defaults to wall clock × GPU count)")
    ap.add_argument("--minutes", type=float, default=1.0, help="--sim: minutes per arm")
    ap.add_argument("--sleep", type=float, default=0.2, help="--sim: simulated run seconds")
    ap.add_argument("--model", default=None, help="a dated model identifier (real)")
    ap.add_argument("--max-budget-usd", type=float, default=None, help="Claude dollar cap per agent (real)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--execute", action="store_true", help="real: start the agents (costs GPU time and API money)")
    ap.add_argument("--repetition", type=int, default=1, help="which repetition of the pre-registered design this is")
    ap.add_argument("--assign-seed", type=int, default=None,
                    help="randomise which GPU group gets which arm (coin flip with this seed; recorded)")
    ap.add_argument("--max-restarts", type=int, default=None,
                    help="restarts per agent that stops early (default 3 real, 0 sim)")
    args = ap.parse_args(argv)
    if not args.sim and (not args.upstream or not args.model or args.max_budget_usd is None):
        ap.error("real mode needs --upstream, --model and --max-budget-usd (see MANUAL_QA.md)")
    for name in ("hours", "gpu_hours_per_arm", "max_budget_usd"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            ap.error(f"--{name.replace('_', '-')} must be positive and finite")
    if args.max_restarts is None:
        args.max_restarts = 0 if args.sim else 3
    elif args.max_restarts < 0:
        ap.error("--max-restarts must be non-negative")
    out = (args.out or ROOT / "bench" / "out" / f"autoresearch-{datetime.now(UTC):%Y%m%d-%H%M%S}").resolve()
    out.mkdir(parents=True, exist_ok=False)
    for arm in ("A", "B"):
        (out / arm).mkdir()
    prep = prepare(args, out)
    if args.sim:
        execute(prep, out, args.minutes * 60 + 120, args.max_restarts)
        from . import post_replicate, report

        post_replicate.main([str(out), "--sim"])
        report.main([str(out)])
        print(f"pipeline check done: {out / 'report.md'}")
        return 0
    print(f"prepared {out}\n  conditions: {out / 'conditions.json'}\n  commands:   {out / 'commands.sh'}")
    if not args.execute:
        print("Nothing was started. Review commands.sh, commit PREREGISTRATION.md, then re-run with --execute.")
        return 0
    codes = execute(prep, out, args.hours * 3600, args.max_restarts)
    stop = json.loads((out / "conditions.json").read_text())["stop"]
    print(f"agents stopped (exit codes {codes}). Next: python -m bench.autoresearch.post_replicate {out} --gpu <n>")
    return int(stop["reason"] == "unaccounted_usage")


if __name__ == "__main__":
    sys.exit(main())
