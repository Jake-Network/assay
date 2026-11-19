"""Arm A in --sim mode: an LLM-free agent that follows upstream program.md (no sharing).

It edits knobs, commits, runs `uv run train.py > run.log 2>&1` (the wrapper shim records it), keeps the commit
when val_bpb improved on its own best, otherwise resets, and logs results.tsv itself, like the upstream loop.
"""
from __future__ import annotations

import argparse
import random
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "examples" / "autoresearch-sim"))
from scripted_agent import GRID, edit, literal  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True).stdout.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    tsv = Path("results.tsv")
    tsv.write_text("commit\tval_bpb\tmemory_gb\tstatus\tdescription\n", encoding="utf-8")
    best: float | None = None
    deadline = time.monotonic() + args.minutes * 60
    first = True
    while time.monotonic() < deadline:
        desc = "baseline"
        if not first:
            src = Path("train.py").read_text(encoding="utf-8")
            k = rng.choice(sorted(GRID))
            v = rng.choice(GRID[k])
            new = edit(src, {k: v})
            if new == src:
                continue
            Path("train.py").write_text(new, encoding="utf-8")
            desc = f"set {k}={literal(v)}"
            git("commit", "-qam", desc)
        subprocess.run("uv run train.py > run.log 2>&1", shell=True)
        out = Path("run.log").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^val_bpb:\s+([\d.]+)", out, re.M)
        mem = re.search(r"^peak_vram_mb:\s+([\d.]+)", out, re.M)
        commit = git("rev-parse", "--short=7", "HEAD")
        if m is None:
            status, val, gb = "crash", 0.0, 0.0
        else:
            val, gb = float(m.group(1)), float(mem.group(1)) / 1024 if mem else 0.0
            status = "keep" if best is None or val < best else "discard"
        with tsv.open("a", encoding="utf-8") as f:
            f.write(f"{commit}\t{val:.6f}\t{gb:.1f}\t{status}\t{desc}\n")
        if status == "keep":
            best = val
        elif not first:
            git("reset", "-q", "--hard", "HEAD~1")
        first = False


if __name__ == "__main__":
    main()
