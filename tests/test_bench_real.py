"""Exercise the real harness offline using the evaluator and synthetic Claude stream records."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench import real
from bench.common import CODE_REF, load_evals
from benchbook import Ledger
from benchbook.hooks import process
from tests.conftest import make_project


def args(**overrides: object) -> SimpleNamespace:
    defaults = {"model": "sonnet", "rounds": 2, "workers": 3, "evals": 4, "max_turns": 20,
                "max_budget_usd": 0.75, "wall_clock": 5, "execute": False}
    return SimpleNamespace(**{**defaults, **overrides})


ROOT = Path(__file__).resolve().parent.parent
CFG = {"init": "hex", "optimizer": "slsqp", "iters": 10, "perturb": 0.0}


def test_real_preview_isolated_and_budgeted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BENCHBOOK_PROJECT", "inherited-project")

    def no_subprocess(*_a: object, **_kw: object) -> None:
        pytest.fail("preview must neither initialize a project nor launch Claude")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    ids = set()
    for mode, root in (("A", "a"), ("B", "b"), ("C", "c1"), ("C", "c2")):
        out = Path(root) / f"{mode}-1"
        assert real.run_real(mode, out, 1, args()) is None
        meta = json.loads((out / "command.json").read_text())
        assert Path(meta["env"]["BENCH_EVALS"]) == (out / "evals.jsonl").resolve()
        assert meta["cmd"][meta["cmd"].index("--max-budget-usd") + 1] == "0.75"
        assert meta["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "sonnet"
        assert meta["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
        if mode == "C":
            ids.add(meta["project_id"])
            assert meta["env"]["BENCHBOOK_PROJECT"] == meta["project_id"]
            assert meta["init"][3] == meta["project_id"]
            assert "--plugin-dir" in meta["cmd"]
        else:
            assert meta["init"] is None and "BENCHBOOK_PROJECT" not in meta["env"]
            assert "--strict-mcp-config" in meta["cmd"] and "--plugin-dir" not in meta["cmd"]
    assert len(ids) == 2


def test_real_evaluator_writes_unique_receipts_from_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("scipy")
    monkeypatch.chdir(tmp_path)
    out = Path("relative") / "A-1"
    prepared = real.prepare("A", out, args())
    command = [sys.executable, "tools/evaluate.py", "--config", json.dumps(CFG), "--seed", "1", "--agent", "worker"]
    receipts = [
        json.loads(subprocess.run(command, cwd=prepared.work, env=prepared.env, capture_output=True, check=True,
                                  text=True).stdout)
        for _ in range(2)
    ]
    assert receipts[0]["eval_id"] != receipts[1]["eval_id"]
    assert all(receipt["run_status"] == "ok" for receipt in receipts)
    rows = load_evals(out / "evals.jsonl")
    assert [row["eval_id"] for row in rows] == [receipt["eval_id"] for receipt in receipts]


def test_capture_keeps_stream_timestamps_and_rejects_process_failure(tmp_path: Path) -> None:
    prepared = real.prepare("A", tmp_path / "A-1", args())
    prepared.cmd = [sys.executable, "-c", """print('{"type":"assistant"}'); print('{"type":"result"}')"""]
    real._capture(prepared, prepared.work.parent, 1)
    events = real._read_stream(prepared.work.parent)
    assert [event["type"] for _, event in events] == ["assistant", "result"]
    assert events[1][0] >= events[0][0] > 0

    prepared.cmd = [sys.executable, "-c", "raise SystemExit(17)"]
    with pytest.raises(real.BenchRunError, match="exited with status 17"):
        real._capture(prepared, prepared.work.parent, 1)


def receipt(n: int, seed: int, score: float | None, *, config: dict | None = None) -> dict:
    return {"eval_id": f"{n:032x}", "config": config or CFG, "code_ref": CODE_REF, "seed": seed, "value": score,
            "run_status": "ok" if score is not None else "invalid"}


def write_run(out: Path, evals: list[dict], final_report: list[dict], **usage_overrides: object) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = [dict(row, finished_ns=(i + 1) * 100) for i, row in enumerate(evals)]
    (out / "evals.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    top = {"id": "lead", "usage": {"input_tokens": 110, "cache_read_input_tokens": 20, "output_tokens": 5}}
    sub = {"id": "worker", "usage": {"input_tokens": 900, "output_tokens": 10}}
    final = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.1,
             "usage": {"input_tokens": 1010, "cache_read_input_tokens": 20, "output_tokens": 15},
             "structured_output": {"summary": "done", "evaluations": final_report}}
    final.update(usage_overrides)
    stream = [{"type": "assistant", "message": top},
              {"type": "assistant", "message": sub, "parent_tool_use_id": "tool-1"},
              {"type": "assistant", "message": sub, "parent_tool_use_id": "tool-1"}, final]
    (out / "lead.stream.jsonl").write_text("".join(json.dumps(row) + "\n" for row in stream))
    (out / "lead.timestamps").write_text("150\n250\n251\n500\n")


def test_real_parser_measures_ids_losses_and_usage(tmp_path: Path) -> None:
    first = receipt(1, 0, 2.60000)
    second = receipt(2, 1, 2.60001)
    failed = receipt(3, 5, None, config={**CFG, "init": "random"})
    out = tmp_path / "A-1"
    write_run(out, [first, second, failed], [first, failed, dict(second, value=first["value"])])
    result = real.parse("A", out, 1, None, 1.0)
    assert result.evaluations == 3 and result.lost_results == 1
    assert result.duplicate_ratio == pytest.approx(2 / 3)
    assert result.lead_context_max == 130 and result.tokens_total == 1045
    assert result.curve == [(135, 2.6), (1045, 2.60001)]
    assert result.cost_usd == 0.1

    results = out / "work" / "results"
    results.mkdir(parents=True)
    (results / "second.json").write_text(json.dumps(second))
    (results / "malformed.json").write_text("unfinished evidence")
    assert real.parse("B", out, 1, None, 1.0).lost_results == 0


def test_real_parser_uses_only_endpoint_when_usage_differs(tmp_path: Path) -> None:
    score = receipt(1, 0, 2.5)
    write_run(tmp_path, [score], [score], usage={"input_tokens": 1500, "output_tokens": 10})
    parsed = real.parse("A", tmp_path, 1, None, 1.0)
    assert parsed.curve == [(1510, 2.5)]
    assert parsed.curve_source == "final aggregate only"


@pytest.mark.parametrize(("patch", "error"), [
    ({"total_cost_usd": 2.0}, "exceeded"),
    ({"usage": {}}, "invalid final"),
    ({"subtype": "error_max_budget_usd", "is_error": True}, "did not finish"),
    ({"structured_output": None}, "missing structured"),
])
def test_real_parser_rejects_invalid_run(tmp_path: Path, patch: dict, error: str) -> None:
    score = receipt(1, 0, 2.5)
    write_run(tmp_path, [score], [score], **patch)
    with pytest.raises(real.BenchRunError, match=error):
        real.parse("A", tmp_path, 1, None, 1.0)


def test_real_parser_rejects_missing_evaluations(tmp_path: Path) -> None:
    write_run(tmp_path, [], [])
    with pytest.raises(real.BenchRunError, match="no evaluations"):
        real.parse("A", tmp_path, 1, None, 1.0)


def test_real_parser_checks_ledger_hook_and_final_report(tmp_path: Path, db_url: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    pid = make_project(db_url, metric="sum_radii")
    worker = Ledger.open(project=pid, agent="worker")
    first = receipt(1, 0, 2.6000)
    second = receipt(2, 1, 2.60001)
    invalid = receipt(3, 5, None, config={**CFG, "init": "random"})
    captured = receipt(4, 7, 2.2)
    missing = receipt(5, 0, None, config={**CFG, "init": "grid"})
    write_run(tmp_path, [first, second, invalid, captured, missing], [second])
    for row in (first, invalid):
        recorded = worker.record_experiment(
            row["config"], {"sum_radii": row["value"]} if row["value"] is not None else {},
            seed=row["seed"], code_ref=row["code_ref"], run_status=row["run_status"],
            trace_ref={"system": "benchbook-bench", "trace_id": row["eval_id"]},
        )
        assert recorded.id is not None
    body = json.dumps({"evaluations": [captured, dict(missing, run_status="timeout")]})
    assert process("subagent-stop", {"last_assistant_message": body}, project=pid).startswith("captured:")
    result = real.parse("C", tmp_path, 1, pid, 1.0)
    assert result.lost_results == 1


def _fake_claude(tmp_path: Path, version: str) -> Path:
    """A claude that answers --version and fails any other call (a paid call would be one)."""
    claude = tmp_path / "bin" / "claude"
    claude.parent.mkdir(exist_ok=True)
    claude.write_text(f"#!/bin/sh\n[ \"$1\" = --version ] || exit 99\nprintf '%s\\n' '{version} (Claude Code)'\n",
                      encoding="utf-8")
    claude.chmod(0o755)
    return claude


@pytest.mark.parametrize("driver", ["bench.run", "bench.code.run", "bench.boundary.run"])
def test_a_different_claude_version_stops_every_driver_before_a_paid_call(tmp_path: Path, driver: str) -> None:
    """2026-11-24: the installed CLI updated itself from 2.1.330 to 2.1.331 mid-sweep and nothing stopped the runs."""
    claude = _fake_claude(tmp_path, "2.1.331")
    out = tmp_path / "out"
    common = ["--execute", "--claude-version", "2.1.330", "--claude-bin", str(claude), "--out", str(out)]
    extra = {"bench.run": ["--configs", "A", "--runs", "1"], "bench.code.run": [],
             "bench.boundary.run": ["--arms", "A", "--model", "m", "--max-budget-usd", "1", "--probe-budget-usd", "1",
                                    "--total-budget-usd", "1", "--tasks", str(_boundary_tasks(tmp_path)),
                                    "--registration", str(_registration(tmp_path))]}[driver]
    p = subprocess.run([sys.executable, "-m", driver, *common, *extra], capture_output=True, text=True, timeout=120,
                       cwd=ROOT)
    assert p.returncode == 1, p.stdout + p.stderr
    assert "--claude-version 2.1.330" in p.stderr and "2.1.331" in p.stderr
    assert not out.exists()


def test_pin_claude_checks_the_number_and_warns_without_a_pin(tmp_path: Path,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    claude = _fake_claude(tmp_path, "2.1.330")
    pinned = argparse.Namespace(claude_bin=str(claude), claude_version="2.1.330")
    assert real.pin_claude(pinned) == "2.1.330 (Claude Code)" and capsys.readouterr().err == ""
    assert real.pin_claude(argparse.Namespace(claude_bin=str(claude))) == "2.1.330 (Claude Code)"
    assert "no --claude-version" in capsys.readouterr().err
    with pytest.raises(ValueError, match="--claude-version 2.1.331: "):
        real.pin_claude(argparse.Namespace(claude_bin=str(claude), claude_version="2.1.331"))
    with pytest.raises(argparse.ArgumentTypeError):
        real.version_arg("2.1")
    assert real.claude_bin(argparse.Namespace()) == "claude"


@pytest.mark.parametrize("requested", [None, "./bin/claude"])
def test_run_uses_the_pinned_executable_in_a_different_cwd_and_path(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested: str | None) -> None:
    monkeypatch.chdir(tmp_path)
    claude = _fake_claude(tmp_path, "2.1.330")
    monkeypatch.setenv("PATH", f"{claude.parent}{os.pathsep}{os.environ['PATH']}")
    options = args(claude_bin=requested, claude_version="2.1.330")
    assert real.pin_claude(options) == "2.1.330 (Claude Code)"

    prepared = real.prepare("A", tmp_path / "trial", options)
    shadow = _fake_claude(prepared.work, "2.1.331")
    prepared.env["PATH"] = str(shadow.parent)
    prepared.cmd[1:] = ["--version"]
    real._capture(prepared, prepared.work.parent, 1)

    assert prepared.cmd[0] == str(claude.resolve())
    assert (prepared.work.parent / "lead.stream.jsonl").read_text() == "2.1.330 (Claude Code)\n"


def _boundary_tasks(tmp_path: Path) -> Path:
    from tests.test_bench_boundary import task_manifest

    return task_manifest(tmp_path)


def _registration(tmp_path: Path) -> Path:
    from tests.test_bench_boundary import registration

    return registration(tmp_path)


def test_worker_models_are_counted_and_off_runs_listed_apart(tmp_path: Path) -> None:
    """30-C-1: the lead passed model "haiku" to all 100 Agent calls, which won over CLAUDE_CODE_SUBAGENT_MODEL;
    30-C-2 ran 28 of 90 workers. Both stay in the table and are listed under it."""
    from bench.report import write_report

    def assistant(content: list[dict[str, object]], *, parent: str | None = None, model: str = "claude-sonnet-5-5",
                  mid: str = "m") -> tuple[int, dict[str, object]]:
        return 0, {"type": "assistant", "parent_tool_use_id": parent,
                   "message": {"id": mid, "model": model, "content": content}}

    call = {"type": "tool_use", "name": "Agent", "id": "t1", "input": {"prompt": "p", "model": "haiku"}}
    events = [assistant([call], mid="lead-1"), assistant([{"type": "text", "text": "a"}], mid="lead-1"),
              assistant([{"type": "tool_use", "name": "Agent", "id": "t2", "input": {"prompt": "p"}}], mid="lead-2"),
              assistant([{"type": "text", "text": "w"}], parent="t1", model="claude-haiku-5-5", mid="w1"),
              assistant([{"type": "tool_use", "name": "Bash", "id": "b", "input": {}}], parent="t1",
                        model="claude-haiku-5-5", mid="w1"),
              assistant([{"type": "text", "text": "w"}], parent="t2", mid="w2"),
              assistant([{"type": "text", "text": "API error"}], parent="t2", model="<synthetic>", mid="e1")]
    assert real.model_counts(events) == ({"claude-haiku-5-5": 1, "claude-sonnet-5-5": 1}, {"(none)": 1, "haiku": 1})

    def row(config: str, run: int, **extra: object) -> dict[str, object]:
        return {"config": config, "run": run, "evaluations": 1, "duplicate_ratio": 0, "lost_results": 0,
                "lead_context_max": 1, "tokens_total": 1, "final_best": None, "curve": [],
                "subagent_models": {"claude-sonnet-5-5": 300}, "agent_model_params": {"(none)": 90, "sonnet": 3},
                "delivered_worker_reports": 90, **extra}

    runs = [row("A", 1), row("C", 1, subagent_models={"claude-haiku-5-5": 360}, agent_model_params={"haiku": 100}),
            row("C", 2, delivered_worker_reports=28)]
    cond = {"dry": False, "model": "sonnet", "workers": 30, "rounds": 3}
    (tmp_path / "metrics.json").write_text(json.dumps({"conditions": cond, "runs": runs}))
    text = write_report(tmp_path).read_text()
    apart = text.split("## Runs to read apart\n\n", 1)[1].split("\n\n", 2)[1].splitlines()
    assert apart == ["- C-1: subagent messages on another model than sonnet: claude-haiku-5-5 360 of 360; the lead's "
                     "Agent calls passed model: haiku ×100",
                     "- C-2: 28 delivered worker reports of 90 requested (workers × rounds)"]
    assert "| C benchbook | 2 |" in text  # not excluded
