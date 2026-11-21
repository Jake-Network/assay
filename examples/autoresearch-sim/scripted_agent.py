"""An LLM-free agent that follows program.benchbook.md's loop with the real `benchbook ar` CLI (§12).

Run it from a git worktree of the simulator checkout with BENCHBOOK_AGENT and BENCHBOOK_PROJECT set:
    python scripted_agent.py --minutes 1 --seed 3
It changes one or two knobs at random. With probability --overlap it deliberately picks the knob change
another agent is working on right now, to exercise intent conflicts.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

GRID: dict[str, list[Any]] = {
    "MATRIX_LR": [0.02, 0.03, 0.05, 0.06, 0.08],
    "EMBEDDING_LR": [0.4, 0.8, 0.9, 1.2],
    "WARMDOWN_RATIO": [0.3, 0.6, 0.7, 0.8],
    "DEPTH": [6, 9, 10, 12, 14],
    "TOTAL_BATCH_SIZE": [2**18, 2**20],
    "WEIGHT_DECAY": [0.05, 0.1, 0.3],
    "WINDOW_PATTERN": ["SSLL", "SL", "L"],
}


def benchbook_cmd() -> list[str]:
    if env := os.environ.get("BENCHBOOK_BIN"):
        return env.split()
    return [sys.executable, "-m", "benchbook"]


def ar(*args: str, json_out: bool = False) -> tuple[int, str]:
    cmd = benchbook_cmd() + (["--json"] if json_out else []) + ["ar", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    return r.returncode, (r.stdout + r.stderr).strip()


def git(*args: str) -> None:
    subprocess.run(["git", *args], capture_output=True, check=False)


def literal(v: Any) -> str:
    return json.dumps(v) if isinstance(v, str) else repr(v)


def current_knobs(src: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"^([A-Z][A-Z0-9_]*) = ([^#\n]+)", src, re.M)}


def edit(src: str, changes: dict[str, Any]) -> str:
    for k, v in changes.items():
        src = re.sub(rf"^({k} = )[^#\n]*?(\s*(#.*)?)$", lambda m, v=v: m.group(1) + literal(v) + m.group(2),
                     src, count=1, flags=re.M)
    return src


def in_flight_knobs() -> list[dict[str, Any]]:
    rc, out = ar("status", json_out=True)
    try:
        data = json.loads(out.splitlines()[-1])
        return [f["knobs"] for p in data.get("partitions", []) for f in p.get("in_flight", []) if f.get("knobs")]
    except (ValueError, IndexError, KeyError):
        return []


def pick(rng: random.Random, src: str, overlap: float) -> dict[str, Any]:
    if rng.random() < overlap:
        others = in_flight_knobs()
        if others:
            return dict(rng.choice(others))
    cur = current_knobs(src)
    names = rng.sample(sorted(GRID), k=rng.choice([1, 1, 2]))
    out: dict[str, Any] = {}
    for n in names:
        options = [v for v in GRID[n] if literal(v) != cur.get(n, "").strip()]
        if options:
            out[n] = rng.choice(options)
    return out or {"MATRIX_LR": rng.choice(GRID["MATRIX_LR"])}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--overlap", type=float, default=0.3)
    ap.add_argument("--cmd", default=f"{sys.executable} train.py")
    ap.add_argument("--drain-seconds", type=float, default=120.0)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    log: list[str] = []
    deadline = time.monotonic() + args.minutes * 60
    train = Path("train.py")

    def say(line: str) -> None:
        log.append(line)
        print(f"[{os.environ.get('BENCHBOOK_AGENT')}] {line}", flush=True)

    # the first run of a fresh lab is the baseline; others are refused (claim conflict or duplicate)
    rc, out = ar("claim", "baseline")
    if rc == 0:
        rc, out = ar("run", "-m", "baseline", "--cmd", args.cmd)
        say(f"baseline -> {out.splitlines()[-1] if out else rc}")
    while time.monotonic() < deadline:
        rc, out = ar("replicate", "--auto", "--cmd", args.cmd)
        if rc == 0:
            say("replicated: " + out.replace("\n", " | "))
        ar("sync")
        ar("brief")
        claimed = None
        for _ in range(5):
            change = pick(rng, train.read_text(encoding="utf-8"), args.overlap)
            desc = "set " + ", ".join(f"{k}={literal(v)}" for k, v in change.items())
            knob_args = [x for k, v in change.items() for x in ("--knob", f"{k}={literal(v)}")]
            rc, out = ar("claim", desc, *knob_args)
            if rc == 0:
                claimed = (change, desc)
                break
            say(f"claim refused: {out.splitlines()[0] if out else rc}")
        if claimed is None:
            continue
        change, desc = claimed
        src = train.read_text(encoding="utf-8")
        new = edit(src, change)
        if new == src:
            continue
        train.write_text(new, encoding="utf-8")
        git("commit", "-qam", desc)
        rc, out = ar("run", "-m", desc, "--cmd", args.cmd)
        line = out.splitlines()[-1] if out else str(rc)
        say(f"{desc} -> {line}")
        if rc != 0 or not line.startswith("KEEP"):
            git("reset", "-q", "--hard", "HEAD~1")
        elif rng.random() < 0.2:
            ar("note", f"{desc} looked promising ({line.split()[2] if len(line.split()) > 2 else ''})")
    end = time.monotonic() + args.drain_seconds
    while time.monotonic() < end:
        rc, out = ar("replicate", "--auto", "--cmd", args.cmd)
        if rc == 3:
            break
        say("drain: " + out.replace("\n", " | "))


if __name__ == "__main__":
    main()
