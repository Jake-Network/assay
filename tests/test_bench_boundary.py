"""Boundary benchmark measurement and command assembly with no provider calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bench import real
from bench.boundary import arms, probes
from bench.run import worker_counts

FIXTURES = Path(__file__).parent / "fixtures" / "bench"


def test_dry_pipeline_measures_every_hypothesis(tmp_path: Path) -> None:
    forbidden = tmp_path / "claude"
    forbidden.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    forbidden.chmod(0o755)
    out = tmp_path / "dry"
    cli = subprocess.run([sys.executable, "-m", "bench.boundary.run", "--dry", "--out", str(out)],
                         env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
                         capture_output=True, text=True, check=True, timeout=60)
    assert str(out / "report.md") in cli.stdout
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "(DRY RUN)" in report
    for column in ("H1 Compaction answers", "H1 Session answers", "H2 False completions", "H2 Weakened merges",
                   "H3 Repeated failures", "H4 Cost USD", "H4 Session tokens", "H4 Wall seconds",
                   "H4 Peak lead tokens", "H5 fan-in", "H6 Trusted stale", "H6 Misflagged fresh",
                   "Delivered reports", "Lead report tokens (estimate)"):
        assert column in report
    data = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert [trial["arm"] for trial in data["trials"]] == list(arms.ARMS)
    assert {row["workers"] for row in data["fan_in"]} == {8, 16, 30}
    assert all(row["workers"] == row["delivered_worker_reports"] for row in data["fan_in"])
    by_arm = {row["arm"]: row for row in data["trials"]}
    baseline = by_arm["A"]
    assert len(baseline["probes"]) == 4
    assert [p["boundary"] for p in baseline["probes"]] == [
        "delayed compaction", "resumed session", "compaction", "resumed session",
    ]
    assert sum(p["false_done"] for p in baseline["phases"]) == 2
    assert sum(p["action"]["trusted_stale"] for p in baseline["phases"]) == 2
    plugin = by_arm["F"]
    assert [p["boundary"] for p in plugin["probes"]] == [
        "delayed compaction", "fresh session", "compaction", "fresh session",
    ]
    assert all(p["injection"]["before"] != p["injection"]["head"] for p in plugin["phases"])
    assert all(p["correct"]["fresh"] for p in plugin["probes"])
    first = out / "scripted" / "F-1"
    compact = json.loads((first / "phase-01-compact" / "command.json").read_text(encoding="utf-8"))
    restart = json.loads((first / "phase-01-restart" / "command.json").read_text(encoding="utf-8"))
    action = json.loads((first / "phase-01-action" / "command.json").read_text(encoding="utf-8"))
    assert "--resume" in compact["cmd"] and "--session-id" in restart["cmd"] and "--resume" in action["cmd"]
    assert compact["session"] != restart["session"] == action["session"]
    assert (out / "scripted" / "E-1" / "past-events" / "session-01" / "lead.jsonl").is_file()
    # The scripted receipts are cumulative per session, as Claude Code's are; each call counts only its own tokens.
    assert all(1650 < call["tokens"] < 1700 for trial in data["trials"] for call in trial["calls"])


def test_a_resumed_call_counts_only_its_own_spend(tmp_path: Path) -> None:
    """Claude Code's receipt for a resumed session reports the whole session's spend: $0.40, then $0.45 after a
    call that cost $0.05. That call is within a $0.10 cap, and the batch spent $0.45, not $0.85."""
    import argparse

    from bench.boundary.run import Batch

    receipts = iter([(0.40, 1000), (0.45, 1300)])

    def execute(prepared: real.PreparedRun, target: Path, _wall: int) -> None:
        cost, tokens = next(receipts)
        events = [{"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
                  {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost,
                   "modelUsage": {"m": {"inputTokens": tokens, "outputTokens": 0}}}]
        (target / "lead.stream.jsonl").write_text(
            "".join(json.dumps({**e, "session_id": prepared.session_id}) + "\n" for e in events), encoding="utf-8")
        (target / "lead.timestamps").write_text("1\n2\n", encoding="utf-8")

    args = argparse.Namespace(probe_budget_usd=0.5, max_budget_usd=0.5, total_budget_usd=10.0, model="m",
                              claude_mem_plugin=None, probe_turns=1, max_turns=1, wall_clock=1)
    batch = Batch(args, tmp_path, execute)
    batch.capture("A", tmp_path, {}, tmp_path / "first", "work", "s-1", resume=False, probe=False)
    args.max_budget_usd = 0.10
    batch.capture("A", tmp_path, {}, tmp_path / "second", "work", "s-1", resume=True, probe=False)
    second = json.loads((tmp_path / "second" / "metrics.json").read_text(encoding="utf-8"))
    assert round(second["cost_usd"], 6) == 0.05 and second["tokens"] == 300
    assert (second["session_cost_usd"], second["session_tokens"]) == (0.45, 1300)
    assert round(batch.spent, 6) == 0.45


def test_configuration_has_equal_worker_instructions_and_read_only_probes(tmp_path: Path) -> None:
    assert arms.ARMS["B"].instructions == arms.ARMS["F"].instructions
    assert not arms.ARMS["A"].fresh_sessions
    assert "claude-progress.txt" in arms.ARMS["C"].instructions
    assert "feature-list.json" in arms.ARMS["C"].instructions
    common = {"resume": True, "model": "model", "budget": 0.5, "max_turns": 10, "claude_mem": tmp_path}
    memory = arms.command("D", "p", "session-id", **common)
    assert memory[memory.index("--plugin-dir") + 1] == str(tmp_path)
    feed = arms.command("E", "p", "session-id", schema=probes.SCHEMA, **common)
    permissions = feed[feed.index("--allowedTools") + 1]
    assert "search_transcript.py" in permissions
    assert "Bash(git log *)" in permissions
    assert "Agent" not in permissions and "Edit" not in permissions and "Write" not in permissions


def test_h5_report_tokens_use_lead_facing_results() -> None:
    baseline = real.worker_reports(real._read_stream(FIXTURES / "A-1"))
    plugin = real.worker_reports(real._read_stream(FIXTURES / "C-1"))
    assert baseline == (74, 1)
    assert plugin == (38, 1)
    events = real._read_stream(FIXTURES / "A-1")
    assert real.worker_reports([*events, *events]) == baseline


def test_h5_worker_sweep_previews_distinct_widths(tmp_path: Path) -> None:
    assert worker_counts("16") == [16]
    out = tmp_path / "h5"
    subprocess.run([sys.executable, "-m", "bench.run", "--workers", "8,16,30", "--configs", "A",
                    "--runs", "1", "--out", str(out)], capture_output=True, check=True, timeout=60)
    for count in (8, 16, 30):
        receipt = out / f"workers-{count}" / "A-1" / "command.json"
        cmd = json.loads(receipt.read_text(encoding="utf-8"))["cmd"]
        assert f"{count} general-purpose subagents" in cmd[2]


def test_execute_requires_filled_registration_before_launch(tmp_path: Path) -> None:
    oracle = tmp_path / "oracle.py"
    oracle.touch()
    source = tmp_path / "task"
    source.mkdir()
    phases = [{"prompt": "Implement.", "stale_path": "stale.txt", "stale_piece": "stale", "fresh_piece": "fresh",
               "injection": "# changed", "action": "Choose which checks to use."}] * 2
    manifest = tmp_path / "tasks.json"
    manifest.write_text(json.dumps([{"id": "task", "base": "a" * 40, "source": "task", "oracle": "oracle.py",
                                     "phases": phases}]), encoding="utf-8")
    out = tmp_path / "run"
    result = subprocess.run([sys.executable, "-m", "bench.boundary.run", "--execute", "--arms", "A",
                             "--tasks", str(manifest), "--out", str(out), "--model", "model", "--max-budget-usd",
                             "1", "--probe-budget-usd", "1", "--total-budget-usd", "1"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "fill in the pre-registration field Model" in result.stderr
    assert not out.exists()
