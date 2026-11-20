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
    from bench.autoresearch import arms

    runs = arms.load_runs(out, "A")
    assert runs and all(r["finished"] and {"code_hash", "val_bpb", "exit", "agent"} <= set(r) for r in runs)
    b = arms.load_runs(out, "B")  # B1: arm B's runs go through the same shim
    assert b and {r["assay_run"] for r in b} <= {"run", "replicate", "calibration"}
    assert cond["baseline_code_hash"] in {r["code_hash"] for r in b}
    report = (out / "report.md").read_text()
    assert "pipeline check, not evidence" in report and "| 3. duplicate runs" in report
    assert "| H1. runs started but not recorded | n/a (no ledger) | 0 (+0 started outside assay ar) |" in report
    nums = json.loads((out / "report.json").read_text())
    assert nums["unrecorded_B"] == {"not_recorded": 0, "outside_assay": 0}
    post = json.loads((out / "post_replicate.json").read_text())
    assert "A_false_best_ratio" in post and "B_false_best_ratio" in post  # B2: one procedure for both arms
    assert (out / "commands.sh").exists()


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



def _run(**kw: object) -> dict[str, object]:
    base = {"event": "start", "agent": "gpu0", "commit": "", "cwd": ".", "assay_run": None, "seed": None}
    return {**base, **kw}


def test_counts_are_the_same_for_both_arms(tmp_path: Path) -> None:
    """B1/B4: duplicates per (code, replication number), baseline runs apart, starts without an end counted."""
    from bench.autoresearch import arms

    (tmp_path / "B").mkdir()
    lines = [
        _run(id="1", start=0.0, code_hash="base", assay_run="run", seed=0),
        {"event": "end", "id": "1", "end": 60.0, "exit": 0, "killed": False, "val_bpb": 1.0},
        _run(id="2", start=1.0, code_hash="base", assay_run="calibration", seed=1),
        {"event": "end", "id": "2", "end": 61.0, "exit": 0, "killed": False, "val_bpb": 1.0},
        _run(id="3", start=2.0, code_hash="x", assay_run="run", seed=0),
        {"event": "end", "id": "3", "end": 62.0, "exit": 0, "killed": False, "val_bpb": 0.9},
        _run(id="4", start=3.0, code_hash="x", assay_run="replicate", seed=1),
        {"event": "end", "id": "4", "end": 63.0, "exit": 0, "killed": False, "val_bpb": 0.9},
        _run(id="5", start=4.0, code_hash="x"),  # the agent ran uv run train.py itself: a duplicate of x/0
        {"event": "end", "id": "5", "end": 124.0, "exit": 0, "killed": False, "val_bpb": 0.91},
        _run(id="6", start=5.0, code_hash="y", assay_run="run", seed=0),
        {"event": "end", "id": "6", "end": 6.0, "exit": None, "killed": True, "val_bpb": None},
        _run(id="7", start=7.0, code_hash="z", assay_run="run", seed=0),  # SIGKILL: no end line
    ]
    (tmp_path / "B" / "runs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    runs = arms.load_runs(tmp_path, "B")
    assert len(runs) == 7 and [r["finished"] for r in runs] == [True] * 6 + [False]
    d = arms.duplicates(runs, "base")
    assert d["duplicates"] == 1 and d["baseline_runs"] == 2 and d["minutes"] == 2.0
    ledger = [{"code_ref": "base", "seed": 0}, {"code_ref": "base", "seed": 1}, {"code_ref": "x", "seed": 0},
              {"code_ref": "x", "seed": 1}, {"code_ref": "y", "seed": 0}]
    assert arms.unrecorded(runs, ledger) == {"not_recorded": 1, "outside_assay": 1}  # z never reached the ledger


def test_shim_records_a_killed_run(tmp_path: Path) -> None:
    """B1: a run killed at the deadline still leaves a start line and a "killed" end line."""
    import os
    import shutil
    import signal
    import subprocess
    import sys
    import time

    from bench.autoresearch import launch

    if sys.platform == "win32":
        pytest.skip("bash shim")
    shim = launch.install_shim(tmp_path)
    (tmp_path / "train.py").write_text("X = 1\n")
    log = tmp_path / "runs.jsonl"
    env = dict(os.environ, PATH=f"{shim}{os.pathsep}{os.environ['PATH']}", AR_BENCH_LOG=str(log),
               AR_BENCH_AGENT="gpu0", AR_BENCH_PY=sys.executable,
               AR_BENCH_HELPER=str(Path(launch.HERE) / "shim_log.py"), AR_BENCH_REAL_CMD="sleep 30",
               AR_BENCH_REAL=shutil.which("true") or "true", ASSAY_AR_RUN="run", ASSAY_AR_SEED="0")
    p = subprocess.Popen(["uv", "run", "train.py"], cwd=tmp_path, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if log.exists() and log.read_text().strip():
            break
        time.sleep(0.1)
    time.sleep(0.3)
    os.killpg(p.pid, signal.SIGTERM)
    assert p.wait(timeout=20) != 0
    events = [json.loads(x) for x in log.read_text().splitlines()]
    assert [e["event"] for e in events] == ["start", "end"]
    assert events[0]["assay_run"] == "run" and events[0]["seed"] == 0 and events[1]["killed"] is True


def test_agents_that_stop_early_are_restarted(tmp_path: Path) -> None:
    """B6: the restart policy is applied and reported per arm."""
    import sys

    from bench.autoresearch import launch

    for arm in ("A", "B"):
        (tmp_path / arm).mkdir()
    agents = [{"arm": "A", "gpu": 0, "cwd": str(tmp_path), "cmd": [sys.executable, "-c", "pass"], "env": None},
              {"arm": "B", "gpu": 1, "cwd": str(tmp_path), "cmd": [sys.executable, "-c", "import time; time.sleep(9)"],
               "env": None}]
    (tmp_path / "conditions.json").write_text("{}")
    launch.execute({"agents": agents}, tmp_path, 4, max_restarts=2, restart_margin=0)
    cond = json.loads((tmp_path / "conditions.json").read_text())
    assert cond["restarts"] == {"A": 2, "B": 0}
    assert len(cond["restart_log"][0]["restarts"]) == 2


def test_gpu_groups_are_randomised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    """B6: --assign-seed flips which GPU group each arm gets, and the draw is recorded."""
    import random
    import shutil
    import subprocess

    from bench.autoresearch import launch

    monkeypatch.setenv("ASSAY_DATABASE_URL", db_url)
    seed = next(s for s in range(100) if random.Random(s).random() < 0.5)  # a seed that swaps
    up = tmp_path / "upstream"
    up.mkdir()
    sim = Path(__file__).resolve().parent.parent / "examples" / "autoresearch-sim"
    for f in ("prepare.py", "train.py"):
        shutil.copy(sim / f, up / f)
    ident = ["-c", "user.name=t", "-c", "user.email=t@e.invalid"]
    for args in (["init", "-q"], ["add", "-A"], [*ident, "commit", "-qm", "x"]):
        subprocess.run(["git", *args], cwd=up, check=True)
    out = tmp_path / "out"
    assert launch.main(["--upstream", str(up), "--gpus-a", "0", "--gpus-b", "1", "--model", "m-2026-01-01",
                        "--max-budget-usd", "1", "--out", str(out), "--assign-seed", str(seed),
                        "--repetition", "2"]) == 0
    cond = json.loads((out / "conditions.json").read_text())
    assert cond["gpus_a"] == [1] and cond["gpus_b"] == [0] and cond["gpu_assignment"] == {"seed": seed, "swapped": True}
    assert cond["repetition"] == 2 and cond["max_restarts"] == 3
