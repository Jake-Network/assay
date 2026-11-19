"""Smoke tests for the circle-packing demo and the bench --dry pipeline (need numpy + scipy)."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from assay import Ledger

ROOT = Path(__file__).resolve().parent.parent
pytest.importorskip("scipy")


def test_evaluator_rejects_tolerance_exploits() -> None:
    sys.path.insert(0, str(ROOT / "examples" / "circle-packing"))
    from evaluator import TOL, score_circles

    ok = [[0.1 + 0.2 * (k % 5), 0.1 + 0.2 * (k // 5), 0.1] for k in range(25)] + [[0.5, 0.5, 1e-6]]
    assert "invalid" in score_circles(ok)  # the 26th circle overlaps
    grid = [[(2 * (k % 6) + 1) / 12, (2 * (k // 6) + 1) / 10, 1 / 12] for k in range(26)]
    base = score_circles(grid)["sum_radii"]
    # inflate every radius so touching pairs overlap by just under the tolerance: no score is gained
    cheat = [[x, y, r + TOL * 0.4] for x, y, r in grid]
    assert "sum_radii" in score_circles(cheat)
    assert score_circles(cheat)["sum_radii"] <= base + 1e-12
    assert "invalid" in score_circles([[x, y, r + 1e-6] for x, y, r in grid])
    assert "invalid" in score_circles(grid[:25])
    out = subprocess.run([sys.executable, str(ROOT / "examples/circle-packing/evaluator.py"),
                          str(ROOT / "examples/circle-packing/seed_program.py")], capture_output=True, text=True)
    assert abs(json.loads(out.stdout)["sum_radii"] - 2.1667) < 1e-3


def test_scripted_swarm_smoke(db_url: str) -> None:
    pid = f"cp-{uuid.uuid4().hex[:6]}"
    p = subprocess.run([sys.executable, str(ROOT / "examples/circle-packing/scripted_swarm.py"), "--workers", "3",
                        "--minutes", "0.4", "--project", pid], capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stderr[-2000:]
    s = Ledger.open(project=pid, agent="lead", database_url=db_url).status()
    assert s["experiments"]["total"] > 10
    assert s["best"]["value"] > 2.5
    assert s["dedup"]["avoided"] + s["dedup"]["rejected"] > 0


def test_bench_dry(tmp_path: Path) -> None:
    p = subprocess.run([sys.executable, "-m", "bench.run", "--dry", "--runs", "1", "--rounds", "1", "--workers", "2",
                        "--evals", "3", "--out", str(tmp_path / "b")], capture_output=True, text=True, cwd=ROOT,
                       timeout=240)
    assert p.returncode == 0, p.stderr[-2000:]
    report = (tmp_path / "b" / "report.md").read_text()
    assert "DRY RUN" in report and "C assay" in report and "A baseline" in report
    m = json.loads((tmp_path / "b" / "metrics.json").read_text())
    c = [r for r in m["runs"] if r["config"] == "C"][0]
    assert c["lost_results"] == 0 and c["duplicate_ratio"] == 0
