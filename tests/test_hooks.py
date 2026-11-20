"""Hook adapter (§12.3, §18.1): three stored sample inputs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assay import Ledger, hooks
from assay.config import write_config
from assay.hooks import extract_text, process

FX = Path(__file__).parent / "fixtures" / "hooks"


@pytest.fixture
def repo(tmp_path: Path, project: str) -> Path:
    write_config(tmp_path, project)
    return tmp_path


def _load(name: str, **subs: str) -> dict:
    raw = (FX / name).read_text()
    for k, v in subs.items():
        raw = raw.replace("{" + k + "}", v)
    return json.loads(raw)


def _notes(lg: Ledger) -> list[dict]:
    return lg.sql_internal("SELECT * FROM assay.claims WHERE project_id=%s AND 'auto-captured' = ANY(tags)",
                           (lg.project_id,))


def test_output_with_existing_ids_is_ignored(repo: Path, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 1}, {"score": 1})
    c = worker.propose_claim("note", "something")
    data = _load("with_ids.json", EXP=f"e:{str(e.id)[:8]}", CLAIM=f"c:{str(c.id)[:8]}")
    data["cwd"] = str(repo)
    assert process("subagent-stop", data) == "cited"
    assert _notes(worker) == []


def test_output_without_ids_is_captured(repo: Path, worker: Ledger) -> None:
    data = _load("no_ids.json")
    data["cwd"] = str(repo)
    r = process("subagent-stop", data)
    assert r.startswith("captured:c:")
    [note] = _notes(worker)
    assert note["kind"] == "note" and note["author"] == "hook"
    assert note["statement"].startswith("[auto-captured] I tried lattice perturbation")
    assert note["payload"]["source"]["session_id"] == "s2"
    assert note["payload"]["source"]["event"] == "SubagentStop"
    roles = worker.sql_internal("SELECT role FROM assay.agents WHERE project_id=%s AND id='hook'",
                                (worker.project_id,))
    assert roles == [{"role": "harness"}]
    # the same output arriving through a second hook event is not captured twice
    process("post-tool-use", data)
    assert len(_notes(worker)) == 1


def test_output_with_only_unknown_ids_is_captured(repo: Path, worker: Ledger) -> None:
    data = _load("missing_ids.json")
    data["cwd"] = str(repo)
    assert extract_text(data).startswith("Finished; see e:00000000")
    assert process("post-tool-use", data).startswith("captured:")
    assert len(_notes(worker)) == 1


def test_handback_message_is_read() -> None:
    assert extract_text({"tool_name": "SubagentHandback", "tool_input": {"message": "report"}}) == "report"


def test_distinct_hook_reports_with_same_excerpt(repo: Path, worker: Ledger) -> None:
    prefix = "x" * 350
    first = {"cwd": str(repo), "last_assistant_message": prefix + " First conclusion."}
    second = {"cwd": str(repo), "last_assistant_message": prefix + " Second conclusion."}
    captured = process("subagent-stop", first)
    assert process("subagent-stop", second) != captured
    assert process("post-tool-use", first) == captured
    notes = _notes(worker)
    assert len(notes) == 2
    assert {note["statement"] for note in notes} == {"[auto-captured] " + prefix[:300]}
    assert {note["payload"]["text"] for note in notes} == {
        first["last_assistant_message"], second["last_assistant_message"],
    }


def test_spool_retries_with_original_project(repo: Path, worker: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    monkeypatch.setattr(hooks, "RETRY_DELAYS", (0,))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    hooks._write_spool(spool, {"event": "subagent-stop", "project": worker.project_id,
                             "data": {"cwd": str(repo), "last_assistant_message": "Recovered finding."}})
    write_config(repo, "unrelated-project")
    calls = 0

    def flaky_process(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary database failure")
        return process(*args, **kwargs)

    monkeypatch.setattr(hooks, "process", flaky_process)
    hooks.replay(spool)
    assert calls == 2 and list(spool.glob("*.json")) == []
    [note] = _notes(worker)
    assert note["payload"]["text"] == "Recovered finding."


def test_legacy_spool_entry_can_be_replayed(repo: Path, worker: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    path = repo / "legacy-report.json"
    path.write_text(json.dumps({"event": "subagent-stop", "cwd": str(repo),
                               "input": json.dumps({"last_assistant_message": "Pre-upgrade finding."})}))
    assert hooks.process_file(path).startswith("ok:captured:")
    assert not path.exists()
    [note] = _notes(worker)
    assert note["payload"]["text"] == "Pre-upgrade finding."


def test_hook_cli_is_fast_and_always_exits_zero(repo: Path, worker: Ledger) -> None:
    data = _load("no_ids.json")
    data["cwd"] = str(repo)
    data["last_assistant_message"] = "A different finding, captured through the CLI entry point."
    env = {**os.environ, "XDG_STATE_HOME": str(repo / "state")}
    t = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "assay.cli", "hook", "subagent-stop"], input=json.dumps(data),
                       text=True, capture_output=True, cwd=repo, timeout=30, env=env)
    elapsed = time.monotonic() - t
    assert p.returncode == 0, p.stderr
    assert elapsed < 1, f"hook took {elapsed:.2f}s"
    for _ in range(100):
        if len(_notes(worker)) == 1:
            break
        time.sleep(0.1)
    assert len(_notes(worker)) == 1
    # garbage input: still exit 0
    p2 = subprocess.run([sys.executable, "-m", "assay.cli", "hook", "subagent-stop"], input="not json",
                        text=True, capture_output=True, cwd=repo, timeout=30, env=env)
    assert p2.returncode == 0


def _transcript(path: Path, start: datetime, agent: str | None = None) -> Path:
    rows = [{"timestamp": start.isoformat(), "message": {"role": "user", "content": "take a task"}}]
    if agent:
        rows.append({"timestamp": (start + timedelta(seconds=5)).isoformat(), "message": {"content": [
            {"type": "tool_use", "id": "x", "name": "mcp__plugin_assay_assay__assay_record_experiment",
             "input": {"agent": agent, "config": {}}}]}})
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


def _stop(repo: Path, text: str, **kw: object) -> dict:
    return {"cwd": str(repo), "hook_event_name": "SubagentStop", "session_id": "s", "agent_id": "a",
            "agent_type": "assay:assay-worker", "last_assistant_message": text, **kw}


def test_task_id_alone_is_not_a_citation(repo: Path, lead: Ledger, worker: Ledger) -> None:
    [tid] = lead.create_tasks([{"title": "lr sweep"}])
    data = _stop(repo, f"{tid} done, lr=3e-4 gave 2.91")
    assert process("subagent-stop", data).startswith("captured:")


def test_citing_a_record_from_before_this_run_is_captured(repo: Path, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 2}, {"score": 1})
    started = datetime.now(UTC) + timedelta(hours=1)  # the run began after e was recorded
    data = _stop(repo, f"Baseline {'e:' + str(e.id)[:8]} was 1; mine got 1.2 but I did not record it.",
                 agent_transcript_path=str(_transcript(repo / "t.jsonl", started)))
    assert process("subagent-stop", data).startswith("captured:")


def test_citation_must_be_by_this_runs_agent(repo: Path, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 3}, {"score": 1})
    started = datetime.now(UTC) - timedelta(minutes=5)
    other = _stop(repo, f"Recorded {'e:' + str(e.id)[:8]}.",
                  agent_transcript_path=str(_transcript(repo / "o.jsonl", started, agent="worker-2")))
    assert process("subagent-stop", other).startswith("captured:")
    mine = _stop(repo, f"Recorded {'e:' + str(e.id)[:8]}.", agent_id="b",
                 agent_transcript_path=str(_transcript(repo / "m.jsonl", started, agent=worker.agent)))
    assert process("subagent-stop", mine) == "cited"


def test_only_assay_workers_are_captured(repo: Path, worker: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _stop(repo, "Explore results: the config lives in src/.", agent_type="Explore")
    assert process("subagent-stop", data).startswith("skipped:not-a-worker")
    assert _notes(worker) == []
    monkeypatch.setenv("ASSAY_HOOK_AGENTS", "assay-worker,implementer")
    assert process("subagent-stop", {**data, "agent_type": "implementer"}).startswith("captured:")


def test_handback_and_stop_capture_one_report(repo: Path, worker: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    common = {"cwd": str(repo), "session_id": "s9", "agent_id": "a9", "agent_type": "assay:assay-worker"}
    # the SubagentStop entry is spooled first but must not win
    hooks._write_spool(spool, {"event": "subagent-stop", "project": worker.project_id, "data": {
        **common, "hook_event_name": "SubagentStop", "last_assistant_message": "Handed back."}})
    time.sleep(0.05)
    hooks._write_spool(spool, {"event": "subagent-handback", "project": worker.project_id, "data": {
        **common, "hook_event_name": "PostToolUse", "tool_name": "SubagentHandback",
        "tool_input": {"message": "Full report: hex beats lattice by 0.02."}}})
    s = hooks.replay_now(spool)
    assert (s.captured, s.skipped, s.ok) == (1, 1, True)
    [note] = _notes(worker)
    assert note["payload"]["text"].startswith("Full report")


def test_permanent_failures_go_to_dead_and_replay_reports(repo: Path, worker: Ledger,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    monkeypatch.setattr(hooks, "RETRY_DELAYS", (0, 0))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    hooks._write_spool(spool, {"event": "subagent-stop", "project": "no-such-project",
                               "data": {"cwd": str(repo), "last_assistant_message": "Lost on a new machine."}})
    hooks._write_spool(spool, {"event": "subagent-stop", "project": worker.project_id,
                               "data": {"cwd": str(repo), "last_assistant_message": "NUL \x00 inside."}})
    t = time.monotonic()
    hooks.replay(spool)
    assert time.monotonic() - t < 10  # no retry loop for an error a retry cannot fix
    assert list(spool.glob("*.json")) == [] and len(list((spool / "dead").glob("*.json"))) == 1
    [note] = _notes(worker)
    assert note["payload"]["text"] == "NUL  inside."
    s = hooks.replay_now(spool)
    assert (s.requeued, s.processed, s.dead, s.ok) == (1, 0, 1, False)
    assert "no-such-project" in str(s) and "dead" in str(s)
    assert s.as_dict()["errors"]


def test_replay_entry_point_returns_summary(repo: Path, worker: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    hooks._write_spool(spool, {"event": "subagent-stop", "project": worker.project_id,
                               "data": {"cwd": str(repo), "last_assistant_message": "Replayed finding."}})
    s = hooks.main("replay")
    assert s is not None and (s.processed, s.captured, s.ok) == (1, 1, True)


def test_no_processor_spawned_while_one_drains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    with hooks._lock(spool, blocking=False) as got:
        assert got and hooks._locked(spool)
    assert not hooks._locked(spool)


def test_missing_project_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ASSAY_PROJECT", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path), "last_assistant_message": "x"})))
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    hooks.main("subagent-stop")
    assert "no assay project" in (tmp_path / "state" / "assay" / "hook.log").read_text()
