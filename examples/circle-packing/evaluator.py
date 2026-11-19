"""Circle packing evaluator: 26 non-overlapping circles in the unit square, maximise the sum of radii.

The evaluator never trusts a program's own score (Tao's warning about verifier exploits): it takes the
circles, re-checks count, positivity, containment and overlap itself, and computes the sum.

Tolerance rule: violations up to TOL (1e-9) are accepted as floating-point noise, but every radius is
reduced by the largest violation before summing, so the tolerance can never add score. Anything beyond
TOL is invalid.

Usage:  python evaluator.py program.py      (program prints {"circles": [[x, y, r], ...]} to stdout)
Output: {"sum_radii": float} or {"invalid": reason}
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from typing import Any

N = 26
TOL = 1e-9
TIMEOUT_S = 30


def score_circles(circles: Any, n: int = N) -> dict[str, Any]:
    try:
        cs = [(float(c[0]), float(c[1]), float(c[2])) for c in circles]
    except (TypeError, ValueError, IndexError):
        return {"invalid": "circles must be a list of [x, y, r] numbers"}
    if len(cs) != n:
        return {"invalid": f"expected {n} circles, got {len(cs)}"}
    if any(not all(math.isfinite(v) for v in c) for c in cs):
        return {"invalid": "non-finite coordinate"}
    if any(r <= 0 for _, _, r in cs):
        return {"invalid": "radii must be positive"}
    worst = 0.0
    for x, y, r in cs:
        worst = max(worst, r - x, x + r - 1, r - y, y + r - 1)
    for i in range(n):
        xi, yi, ri = cs[i]
        for j in range(i + 1, n):
            xj, yj, rj = cs[j]
            worst = max(worst, (ri + rj) - math.hypot(xi - xj, yi - yj))
    if worst > TOL:
        return {"invalid": f"constraint violated by {worst:.3g} (> {TOL:g})"}
    shrink = max(worst, 0.0)
    return {"sum_radii": sum(r - shrink for _, _, r in cs), "max_violation": worst}


def evaluate_program(path: str) -> dict[str, Any]:
    try:
        p = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"invalid": f"timeout after {TIMEOUT_S}s", "run_status": "timeout"}
    if p.returncode != 0:
        return {"invalid": f"program exited {p.returncode}: {p.stderr.strip()[-300:]}", "run_status": "error"}
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"invalid": "last stdout line must be JSON {\"circles\": [[x, y, r], ...]}", "run_status": "error"}
    res = score_circles(out.get("circles") if isinstance(out, dict) else None)
    if "invalid" in res:
        res["run_status"] = "invalid"
    return res


if __name__ == "__main__":
    print(json.dumps(evaluate_program(sys.argv[1])))
