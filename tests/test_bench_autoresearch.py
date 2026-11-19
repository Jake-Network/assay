"""bench/autoresearch --sim: the launch-experiment pipeline end to end on the CPU simulator (§13.4, A8)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_sim_pipeline(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bench.autoresearch import launch

    monkeypatch.setenv("ASSAY_DATABASE_URL", db_url)
    out = tmp_path / "run"
    assert launch.main(["--sim", "--agents", "2", "--minutes", "0.3", "--out", str(out)]) == 0
    cond = json.loads((out / "conditions.json").read_text())
    assert cond["label"] == "pipeline check, not evidence" and cond["exit_codes"] == [0, 0, 0, 0]
    runs = [json.loads(x) for x in (out / "A" / "runs.jsonl").read_text().splitlines()]
    assert runs and all({"code_hash", "val_bpb", "exit", "agent"} <= set(r) for r in runs)
    report = (out / "report.md").read_text()
    assert "pipeline check, not evidence" in report and "| 3. duplicate runs" in report
    assert (out / "post_replicate.json").exists() and (out / "commands.sh").exists()


def test_real_mode_only_previews(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    import shutil
    import subprocess

    from bench.autoresearch import launch

    monkeypatch.setenv("ASSAY_DATABASE_URL", db_url)
    up = tmp_path / "upstream"
    up.mkdir()
    sim = Path(__file__).resolve().parent.parent / "examples" / "autoresearch-sim"
    for f in ("prepare.py", "train.py"):
        shutil.copy(sim / f, up / f)
    ident = ["-c", "user.name=t", "-c", "user.email=t@e.invalid"]
    for args in (["init", "-q"], ["add", "-A"], [*ident, "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=up, check=True)
    out = tmp_path / "out"
    code = launch.main(["--upstream", str(up), "--gpus-a", "0", "--gpus-b", "1", "--model", "m-2026-01-01",
                        "--max-budget-usd", "1", "--out", str(out)])
    assert code == 0
    cmds = (out / "commands.sh").read_text()
    assert cmds.count("claude -p") == 2 and "--max-budget-usd 1" in cmds and "program.assay.md" in cmds
    assert not (out / "A" / "runs.jsonl").exists()  # nothing ran
