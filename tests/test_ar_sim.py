"""§12 e2e: a scripted swarm on the CPU simulator; the acceptance conditions are asserted by
examples/autoresearch-sim/run_sim_swarm.py (exit 0 = every condition held)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SIM = Path(__file__).resolve().parent.parent / "examples" / "autoresearch-sim" / "run_sim_swarm.py"


def test_sim_swarm(db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ASSAY_DATABASE_URL", db_url)
    spec = importlib.util.spec_from_file_location("run_sim_swarm", SIM)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # a higher lucky-run rate than the default so the refutation path is exercised in a short run
    code = mod.main(["--agents", "3", "--minutes", "1", "--sleep", "0.2", "--lucky", "0.15"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert '"unrecorded": 0' in out and '"violations": []' in out
