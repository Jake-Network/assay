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


def test_slsqp_repairs_overlap_before_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.path.insert(0, str(ROOT / "examples" / "circle-packing"))
    import numpy as np
    import packing
    from evaluator import TOL, score_circles

    grid = packing.initial("grid", 0.0, np.random.default_rng(0))
    grid[2 * packing.N:] += TOL
    assert "invalid" in score_circles(grid.reshape(3, packing.N).T)
    monkeypatch.setattr(packing, "slsqp", lambda z, iters: (grid, False, "Iteration limit reached"))

    circles, info = packing.solve({"init": "grid", "optimizer": "slsqp", "iters": 10}, seed=0)

    result = score_circles(circles)
    assert "invalid" not in result, result
    assert result["max_violation"] == 0.0
    assert info == {"converged": False, "message": "Iteration limit reached"}


def test_scripted_swarm_smoke(db_url: str) -> None:
    pid = f"cp-{uuid.uuid4().hex[:6]}"
    p = subprocess.run([sys.executable, str(ROOT / "examples/circle-packing/scripted_swarm.py"), "--workers", "3",
                        "--minutes", "0.4", "--project", pid], capture_output=True, text=True, timeout=240)
    assert p.returncode == 0, p.stderr[-2000:]
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    s = lead.status()
    assert s["experiments"]["total"] > 10
    assert s["best"]["value"] > 2.5
    assert s["dedup"]["avoided"] + s["dedup"]["rejected"] > 0


def test_scripted_verification_is_limited_to_the_recorded_seeds(lead: Ledger, worker: Ledger) -> None:
    sys.path.insert(0, str(ROOT / "examples" / "circle-packing"))
    from scripted_swarm import CODE_REF, verify_best

    config = {"init": "hex", "optimizer": "slsqp", "iters": 100, "perturb": 0.02}
    replica = lead.as_agent("replica")
    metric = lead.project.primary_metric
    assert metric is not None
    for runner, seed, score in [(worker, 0, 2.61476), (worker, 1, 2.63), (replica, 2, 2.62)]:
        runner.record_experiment(config, {metric: score}, seed=seed, code_ref=CODE_REF)

    progress = lead.sql("SELECT best_so_far FROM v_progress ORDER BY n")["rows"]
    assert [point["best_so_far"] for point in progress] == [2.61476, 2.63, 2.63]
    verify_best(lead)
    first = lead.sql("SELECT id, statement FROM v_claim_status WHERE status='verified'")["rows"]
    assert len(first) == 1
    assert "minimum sum_radii 2.61476 across 3 tested seeds" in first[0]["statement"]

    replica.record_experiment(config, {metric: 2.60}, seed=4, code_ref=CODE_REF)
    verify_best(lead)
    decisions = lead.sql_internal(
        "SELECT oracle_detail FROM assay.decisions WHERE project_id=%s AND new_status='verified' ORDER BY created_at",
        (lead.project_id,),
    )
    assert [d["oracle_detail"]["seeds"] for d in decisions] == [[0, 1, 2], [0, 1, 2, 4]]


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
