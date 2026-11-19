"""Real mode: the lead is Claude Code headless (`claude -p`). This costs money, so the harness prints the
commands by default; pass --execute to run them. Check the flags against `claude --help` for your version
before a paid run (MANUAL_QA.md).

Every configuration gets the same model, the same worker count and rounds, the same --max-turns and the same
wall-clock limit; evaluator calls all go through tools/evaluate.py, which logs to evals.jsonl.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .common import ROOT, RunMetrics, best_curve, duplicate_ratio, load_evals

TASK = ("Maximize the sum of radii of 26 non-overlapping circles in the unit square. Evaluate configurations "
        "with: python tools/evaluate.py --config '<json>' --seed <n> --agent <your-name>. "
        "Config keys: init (grid|hex|random|lattice_perturb), optimizer (slsqp|penalty), iters (10..5000), "
        "perturb (0..0.2).")

LEAD = {
    "A": ("{task} Run {rounds} rounds. In each round launch {workers} subagents in parallel (Agent tool), each "
          "exploring a different part of the space with about {evals} evaluations, and have them report their "
          "results back to you as text. Plan the next round from their reports. Finish with a report listing "
          "every result you know (config, seed, value) and the best one."),
    "B": ("{task} Run {rounds} rounds. In each round launch {workers} subagents in parallel (Agent tool), each "
          "exploring a different part of the space with about {evals} evaluations. Each subagent must write "
          "every result to results/<agent>-<n>.json as soon as it has it. Read results/ to plan the next round. "
          "Finish with a report of the best result."),
    "C": ("/assay:assay-lead {task} Use {rounds} rounds with {workers} workers per round and about {evals} "
          "evaluations per task. Finish with the final report described in the skill."),
}


def prepare(mode: str, out: Path, args: Any) -> tuple[list[str], dict[str, str], Path]:
    work = out / "work"
    if work.exists():
        shutil.rmtree(work)
    (work / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "bench" / "tools" / "evaluate.py", work / "tools" / "evaluate.py")
    env = {**os.environ, "BENCH_ROOT": str(ROOT), "BENCH_EVALS": str(out / "evals.jsonl")}
    prompt = LEAD[mode].format(task=TASK, rounds=args.rounds, workers=args.workers, evals=args.evals)
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", args.model,
           "--max-turns", str(args.max_turns),
           "--allowedTools", "Agent,Bash(python tools/evaluate.py:*),Read,Write,Glob"]
    if mode == "C":
        pid = f"bench-{out.name}"
        subprocess.run(["assay", "init", "--project", pid, "--goal", TASK.split(". Evaluate")[0] + ".",
                        "--metric", "sum_radii", "--direction", "max"], cwd=work, check=True, env=env)
        cmd += ["--plugin-dir", str(ROOT / "integrations" / "claude-code"),
                "--allowedTools", "mcp__plugin_assay_assay__*"]
        env["ASSAY_PROJECT"] = pid
    if mode == "B":
        (work / "results").mkdir()
    return cmd, env, work


def run_real(mode: str, out: Path, run: int, args: Any) -> RunMetrics | None:
    cmd, env, work = prepare(mode, out, args)
    (out / "command.json").write_text(json.dumps({"cmd": cmd, "cwd": str(work)}, indent=2))
    if not args.execute:
        print(f"[{mode} run {run}] cd {work} && " + " ".join(json.dumps(c) if " " in c else c for c in cmd))
        return None
    stream = out / "lead.stream.jsonl"
    with open(stream, "w") as f:
        subprocess.run(cmd, cwd=work, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=args.wall_clock * 60,
                       check=False)
    return parse(mode, out, run)


def parse(mode: str, out: Path, run: int) -> RunMetrics:
    ctx_max, tokens, final_text = 0, 0, ""
    for raw in (out / "lead.stream.jsonl").read_text().splitlines():
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant" and not ev.get("parent_tool_use_id"):
            u = ev.get("message", {}).get("usage", {})
            ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get(
                "cache_creation_input_tokens", 0)
            ctx_max = max(ctx_max, ctx)
        if ev.get("type") == "result":
            final_text = ev.get("result", "") or ""
            u = ev.get("usage", {})
            tokens = sum(int(u.get(k, 0)) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                       "cache_creation_input_tokens"))
    evals = load_evals(out / "evals.jsonl")
    if mode == "C":
        lost = 0  # every evaluation is checked against the ledger below
        try:
            from assay import Ledger

            lg = Ledger.open(project=f"bench-{out.name}", agent="human")
            have = {(json.dumps(r["config"], sort_keys=True), r["seed"]) for r in lg.sql_internal(
                "SELECT config, seed FROM assay.experiments WHERE project_id=%s", (lg.project_id,))}
            lost = sum(1 for e in evals if (json.dumps(e["config"], sort_keys=True), e["seed"]) not in have)
        except Exception:  # noqa: BLE001
            lost = -1
    elif mode == "B":
        lost = sum(1 for e in evals if e["value"] is not None and f"{e['value']:.4f}" not in _files_text(out))
    else:
        lost = sum(1 for e in evals if e["value"] is None or f"{e['value']:.4f}" not in final_text)
    # real runs log tokens_cum=-1; spread the run's total tokens over evaluations by time for the curve
    if evals and tokens:
        t0, t1 = evals[0]["t"], evals[-1]["t"] or evals[0]["t"]
        for e in evals:
            e["tokens_cum"] = int(tokens * ((e["t"] - t0) / max(t1 - t0, 1e-9)))
    curve = best_curve(evals)
    return RunMetrics(mode, run, len(evals), duplicate_ratio(evals), lost, ctx_max, tokens,
                      curve[-1][1] if curve else None, curve)


def _files_text(out: Path) -> str:
    return "\n".join(p.read_text() for p in (out / "work" / "results").glob("*.json"))


__all__ = ["run_real", "parse", "time"]
