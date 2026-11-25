"""Boundary benchmark measurement and command assembly with no provider calls."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bench import real
from bench.boundary import arms, probes, report, run, scripted
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
    calls = [d for d in (out / "scripted").glob("*-1/phase-*") if (d / "command.json").is_file()]
    assert calls and all((d / "transcripts" / "lead.jsonl").is_file() and (d / "transcripts" / "subagents").is_dir()
                         for d in calls)
    # The scripted receipts are cumulative per session, as Claude Code's are; each call counts only its own tokens.
    assert all(1650 < call["tokens"] < 1700 for trial in data["trials"] for call in trial["calls"])


@pytest.mark.parametrize(("session_cost", "exceeded"), [(0.40, False), (0.41, True)])
def test_a_resumed_call_counts_only_its_own_spend(tmp_path: Path, session_cost: float, exceeded: bool) -> None:
    """Accept the exact $0.10 cap despite binary subtraction, and retain a real over-budget receipt."""
    receipts = iter([(0.30, 1000), (session_cost, 1300)])

    def execute(prepared: real.PreparedRun, target: Path, _wall: int) -> None:
        cost, tokens = next(receipts)
        events = [{"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
                  {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost,
                   "modelUsage": {"m": {"inputTokens": tokens, "outputTokens": 0}}}]
        (target / "lead.stream.jsonl").write_text(
            "".join(json.dumps({**e, "session_id": prepared.session_id}) + "\n" for e in events), encoding="utf-8")
        (target / "lead.timestamps").write_text("1\n2\n", encoding="utf-8")

    args = argparse.Namespace(probe_budget_usd=0.1, max_budget_usd=0.5, total_budget_usd=10.0, model="m",
                              claude_mem_plugin=None, probe_turns=1, max_turns=1, wall_clock=1)
    batch = run.Batch(args, tmp_path, execute)
    batch.capture("A", tmp_path, {}, tmp_path / "first", "work", "s-1", resume=False, probe=False)
    if exceeded:
        with pytest.raises(real.BenchRunError, match="second exceeded its cap"):
            batch.capture("A", tmp_path, {}, tmp_path / "second", "probe", "s-1", resume=True, probe=True)
    else:
        batch.capture("A", tmp_path, {}, tmp_path / "second", "probe", "s-1", resume=True, probe=True)
    second = json.loads((tmp_path / "second" / "metrics.json").read_text(encoding="utf-8"))
    assert second["cost_usd"] == pytest.approx(session_cost - 0.30) and second["tokens"] == 300
    assert (second["session_cost_usd"], second["session_tokens"]) == (session_cost, 1300)
    assert json.loads((tmp_path / "spend.json").read_text())["counted_usd"] == session_cost


@pytest.mark.parametrize("restored", [True, False])
def test_a_resumed_call_reads_its_own_usage_whether_or_not_the_session_totals_were_restored(
    tmp_path: Path, restored: bool,
) -> None:
    """Third boundary pilot: a resume reported only its own spend (0.0623 after 0.0602), which the difference rule
    rejected as less than the previous receipt. The call's own usage is in model_usage_since_last_result."""
    own = [(0.0602, 54000), (0.0623, 27113)]
    totals = [own[0], (own[0][0] + own[1][0], own[0][1] + own[1][1]) if restored else own[1]]
    calls = iter(zip(totals, own, strict=True))

    def execute(prepared: real.PreparedRun, target: Path, _wall: int) -> None:
        (cost, tokens), (own_cost, own_tokens) = next(calls)
        events = [{"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
                  {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost,
                   "modelUsage": {"m": {"inputTokens": tokens, "outputTokens": 0, "costUSD": cost}},
                   run.OWN_USAGE: {"m": {"inputTokens": own_tokens, "outputTokens": 0, "costUSD": own_cost}}}]
        (target / "lead.stream.jsonl").write_text(
            "".join(json.dumps({**e, "session_id": prepared.session_id}) + "\n" for e in events), encoding="utf-8")
        (target / "lead.timestamps").write_text("1\n2\n", encoding="utf-8")

    args = argparse.Namespace(probe_budget_usd=0.5, max_budget_usd=3.0, total_budget_usd=10.0, model="m",
                              claude_mem_plugin=None, probe_turns=1, max_turns=1, wall_clock=1)
    batch = run.Batch(args, tmp_path, execute)
    batch.capture("A", tmp_path, {}, tmp_path / "first", "work", "s-1", resume=False, probe=False)
    batch.capture("A", tmp_path, {}, tmp_path / "second", "probe", "s-1", resume=True, probe=True)
    second = json.loads((tmp_path / "second" / "metrics.json").read_text(encoding="utf-8"))
    assert (second["cost_usd"], second["tokens"]) == (0.0623, 27113)
    assert second["session_cost_usd"] == pytest.approx(0.1225) and second["session_tokens"] == 81113
    assert json.loads((tmp_path / "spend.json").read_text())["counted_usd"] == pytest.approx(0.1225)


def test_an_unreadable_own_usage_is_a_missing_receipt() -> None:
    usage = {"input_tokens": 10, "output_tokens": 1}
    events = [(1, {"type": "assistant", "session_id": "s", "message": {"usage": usage}}),
              (2, {"type": "result", "subtype": "success", "is_error": False, "session_id": "s",
                   "total_cost_usd": 0.1, "modelUsage": {"m": {"inputTokens": 5, "costUSD": 0.1}},
                   run.OWN_USAGE: {"m": {"inputTokens": 5}}})]
    with pytest.raises(real.BenchRunError, match="unreadable"):
        run.read_call(events, "s")


def test_every_call_copies_its_session_transcripts(tmp_path: Path) -> None:
    """Arm F's verdicts reach the lead through PostToolUse hooks, which the stream does not show: each call keeps the
    session's transcripts, the lead's and its subagents', in its own folder."""
    config = tmp_path / "claude-config"

    def execute(prepared: real.PreparedRun, target: Path, _wall: int) -> None:
        session = prepared.session_id
        saved = config / "projects" / "-work" / session
        if session != "s-missing":
            (saved / "subagents").mkdir(parents=True, exist_ok=True)
            (saved.parent / f"{session}.jsonl").write_text(f'{{"call": "{target.name}"}}\n', encoding="utf-8")
            (saved / "subagents" / "agent-a1.jsonl").write_text("{}\n", encoding="utf-8")
            (saved / "subagents" / "agent-a1.meta.json").write_text('{"agentType": "general-purpose"}',
                                                                    encoding="utf-8")
        events = [{"type": "assistant", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}},
                  {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01,
                   "modelUsage": {"m": {"inputTokens": 11, "outputTokens": 0}}}]
        (target / "lead.stream.jsonl").write_text(
            "".join(json.dumps({**e, "session_id": session}) + "\n" for e in events), encoding="utf-8")
        (target / "lead.timestamps").write_text("1\n2\n", encoding="utf-8")

    args = argparse.Namespace(probe_budget_usd=0.1, max_budget_usd=0.5, total_budget_usd=10.0, model="m", dry=False,
                              claude_mem_plugin=None, probe_turns=1, max_turns=1, wall_clock=1)
    task = scripted.fixture(tmp_path)
    trial = run.Trial(run.Batch(args, tmp_path, execute), task, "F", tmp_path / "F-1", tmp_path / "work",
                      {"CLAUDE_CONFIG_DIR": str(config)})
    trial.target.mkdir()
    trial.invoke("phase-01", "work", "s-1", resume=False)
    trial.invoke("phase-01-action", "act", "s-1", resume=True)
    for name in ("phase-01", "phase-01-action"):
        copied = trial.target / name / "transcripts"
        assert json.loads((copied / "lead.jsonl").read_text()) == {"call": name}
        assert {p.name for p in (copied / "subagents").iterdir()} == {"agent-a1.jsonl", "agent-a1.meta.json"}
        assert json.loads((trial.target / name / "transcripts.json").read_text())["copy"] == "transcripts"
    trial.invoke("phase-01-restart", "probe", "s-missing", resume=False)  # a session with no transcript
    assert "error" in json.loads((trial.target / "phase-01-restart" / "transcripts.json").read_text())


@pytest.mark.parametrize("completed_b", [True, False])
def test_report_compares_matched_repetitions_and_lists_unmatched(tmp_path: Path, completed_b: bool) -> None:
    def row(arm: str, repetition: int) -> dict[str, object]:
        return {
            "task": "task", "run": repetition, "arm": arm, "wall_seconds": 2, "phases": [], "probes": [],
            "calls": [{"cost_usd": 0.25, "tokens": 20, "kind": "work", "lead_context_max": 10}],
        }

    trials = [row("A", 1), row("A", 2)]
    if completed_b:
        trials.insert(1, row("B", 1))
    data = {"dry": False, "arms": ["A", "B"], "trials": trials, "stopped": "budget", "fan_in": []}
    (tmp_path / "metrics.json").write_text(json.dumps(data), encoding="utf-8")
    output = report.write(tmp_path).read_text(encoding="utf-8")
    rows = [line for line in output.splitlines() if line.startswith(("| A Single session", "| B Text subagents"))]
    if completed_b:
        assert len(rows) == 2 and all("| 1 |" in line and "| 0.2500 | 20 | 2.00 |" in line for line in rows)
    else:
        assert not rows and "No matched trials completed." in output
        assert "| task | 1 | A | B |" in output
    assert "| task | 2 | A | B |" in output
    assert json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))["trials"] == trials


@pytest.mark.parametrize("failure", [None, "init", "trial"])
def test_arm_f_stops_the_trial_database_even_when_init_or_work_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    task = scripted.fixture(tmp_path)
    pg = tmp_path / task.id / "F-1" / "pg"
    calls = []
    actual_run = subprocess.run

    def process(cmd, **kwargs):
        if cmd[:4] == [sys.executable, "-m", "benchbook", "init"]:
            assert kwargs["env"]["BENCHBOOK_DATA_DIR"] == str(pg)
            calls.append("init")
            if failure == "init":
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return actual_run(cmd, **kwargs)

    def work(_trial):
        calls.append("trial")
        if failure == "trial":
            raise real.BenchRunError("trial failed")
        return {}

    def stop(path: Path) -> bool:
        assert path == pg
        calls.append("stop")
        return True

    monkeypatch.setattr(run.subprocess, "run", process)
    monkeypatch.setattr(run.Trial, "run", work)
    monkeypatch.setattr(run, "stop_embedded", stop)
    args = argparse.Namespace(dry=False, compact_window=100000, model="scripted")
    batch = run.Batch(args, tmp_path, scripted.capture)
    if failure:
        with pytest.raises(subprocess.CalledProcessError if failure == "init" else real.BenchRunError):
            run.trial(batch, task, "F", 1)
    else:
        assert run.trial(batch, task, "F", 1)["arm"] == "F"
    assert calls == (["init", "stop"] if failure == "init" else ["init", "trial", "stop"])


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


def task_manifest(tmp_path: Path) -> Path:
    oracle = tmp_path / "oracle.py"
    oracle.touch()
    source = tmp_path / "task"
    source.mkdir()
    phases = [{"prompt": "Implement.", "stale_path": "stale.txt", "stale_piece": "stale", "fresh_piece": "fresh",
               "injection": "# changed", "action": "Choose which checks to use."}] * 2
    manifest = tmp_path / "tasks.json"
    manifest.write_text(json.dumps([{"id": "task", "base": "a" * 40, "source": "task", "oracle": "oracle.py",
                                     "phases": phases}]), encoding="utf-8")
    return manifest


REGISTRATION_FIELDS = ("Model", "Claude Code", "Date", "Budget", "Tasks", "Claude-mem", "Commits", "H5 budget")


def registration(tmp_path: Path, *, blank: str | None = None) -> Path:
    """A pre-registration with every field run.py checks filled in, except blank (left as the template's
    "[...]"), so the tests do not depend on the repository's own registration."""
    path = tmp_path / "registration.md"
    path.write_text("".join(f"**{name}**: {'[to be filled]' if name == blank else 'registered'}\n"
                            for name in REGISTRATION_FIELDS), encoding="utf-8")
    return path


def test_execute_requires_filled_registration_before_launch(tmp_path: Path) -> None:
    manifest = task_manifest(tmp_path)
    out = tmp_path / "run"
    result = subprocess.run([sys.executable, "-m", "bench.boundary.run", "--execute", "--arms", "A",
                             "--tasks", str(manifest), "--registration", str(registration(tmp_path, blank="Model")),
                             "--out", str(out), "--model", "model", "--max-budget-usd", "1", "--probe-budget-usd",
                             "1", "--total-budget-usd", "1"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "fill in the pre-registration field Model" in result.stderr
    assert not out.exists()


@pytest.mark.parametrize("version", ["2.1.276 (Claude Code)", "unknown"])
def test_execute_rejects_unsupported_claude_version_before_trial(tmp_path: Path, version: str) -> None:
    manifest = task_manifest(tmp_path)
    claude = tmp_path / "claude"
    claude.write_text(f"#!/bin/sh\n[ \"$1\" = --version ] || exit 99\nprintf '%s\\n' '{version}'\n", encoding="utf-8")
    claude.chmod(0o755)
    out = tmp_path / "run"
    result = subprocess.run(
        [sys.executable, "-m", "bench.boundary.run", "--execute", "--arms", "A", "--tasks", str(manifest),
         "--registration", str(registration(tmp_path)), "--out", str(out), "--model", "model", "--max-budget-usd", "1",
         "--probe-budget-usd", "1", "--total-budget-usd", "1"],
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert "--execute needs Claude Code 2.1.277 or newer" in result.stderr
    assert not out.exists()
