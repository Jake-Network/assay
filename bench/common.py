"""Shared bench pieces: the evaluation wrapper that logs every evaluator call, and the metrics."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "examples" / "circle-packing"))

from benchbook.dedup import canonical_json  # noqa: E402

SPACE = {"init": ["grid", "hex", "random", "lattice_perturb"], "optimizer": ["slsqp", "penalty"],
         "iters": [10, 30, 100, 300, 1000], "perturb": [0.0, 0.01, 0.02, 0.05]}
SOLVER_DIR = ROOT / "examples" / "circle-packing"
CODE_REF = "packing-" + hashlib.sha256(
    (SOLVER_DIR / "packing.py").read_bytes() + b"\0" + (SOLVER_DIR / "evaluator.py").read_bytes()
).hexdigest()


@cache
def _solve_cached(cfg_json: str, seed: int) -> dict[str, Any]:
    from evaluator import score_circles
    from packing import solve

    cfg = json.loads(cfg_json)
    try:
        circles, info = solve(cfg, seed)
    except Exception as e:  # noqa: BLE001
        return {"run_status": "error", "value": None, "notes": str(e)[:200]}
    res = score_circles(circles)
    if "invalid" in res:
        return {"run_status": "invalid", "value": None, "notes": res["invalid"]}
    note = None if info.get("converged", True) else f"SLSQP: {info.get('message')}"
    return {"run_status": "ok", "value": round(res["sum_radii"], 10), "notes": note}


class EvalLog:
    """Every evaluator call goes through here and lands in evals.jsonl (so duplicates and losses can be
    measured afterwards for every configuration, including the baseline)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "ab", buffering=0)  # noqa: SIM115
        self.n = 0

    def evaluate(self, agent: str, cfg: dict[str, Any], seed: int, tokens_so_far: int) -> dict[str, Any]:
        res = _solve_cached(json.dumps(cfg, sort_keys=True, allow_nan=False), seed)
        self.n += 1
        row = {"i": self.n, "eval_id": uuid.uuid4().hex, "t": time.time(), "finished_ns": time.monotonic_ns(),
               "agent": agent, "config": cfg, "code_ref": CODE_REF, "seed": seed, "tokens_cum": tokens_so_far,
               **res}
        self.f.write((json.dumps(row, allow_nan=False) + "\n").encode())
        return row

    def close(self) -> None:
        self.f.close()


@dataclass
class RunMetrics:
    config: str
    run: int
    evaluations: int
    duplicate_ratio: float
    lost_results: int
    lead_context_max: int
    tokens_total: int
    final_best: float | None
    curve: list[tuple[int, float]]
    cost_usd: float | None = None
    curve_source: str = "simulated"
    # Per-message usage against the final totals (bench.real.usage_check). tokens_total above is the final result
    # event's own `usage`, which covers the lead's API calls; the whole session's total is in here.
    usage_check: dict[str, Any] | None = None


def load_evals(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def duplicate_ratio(evals: list[dict[str, Any]]) -> float:
    """Evaluations whose (config, code) was evaluated 2+ times, over all evaluations."""
    from collections import Counter

    keys = [canonical_json([e["config"], e["code_ref"]]) for e in evals]
    c = Counter(keys)
    return sum(1 for k in keys if c[k] >= 2) / len(keys) if keys else 0.0


def best_curve(evals: list[dict[str, Any]]) -> list[tuple[int, float]]:
    best, out = None, []
    for e in evals:
        if e["value"] is not None and (best is None or e["value"] > best):
            best = e["value"]
        if best is not None:
            out.append((e["tokens_cum"], best))
    return out
