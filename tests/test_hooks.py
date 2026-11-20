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
    ref = f"e:{str(e.id)[:8]}"
    data = _load("with_ids.json", EXP=ref, CLAIM=f"c:{str(c.id)[:8]}")
    data["cwd"] = str(repo)
    data["agent_transcript_path"] = str(_transcript(repo / "t.jsonl", datetime.now(UTC) - timedelta(seconds=6),
                                                    agent=worker.agent, result={"id": ref}))
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


def _transcript(path: Path, start: datetime, agent: str | None = None, result: dict | None = None,
                tool: str = "mcp__plugin_assay_assay__assay_record_experiment") -> Path:
    rows = [{"timestamp": start.isoformat(), "message": {"role": "user", "content": "take a task"}}]
    if agent:
        rows.append({"timestamp": (start + timedelta(seconds=5)).isoformat(), "message": {"content": [
            {"type": "tool_use", "id": "x", "name": tool,
             "input": {"agent": agent, "config": {}}}]}})
    if result:
        rows.append({"timestamp": (start + timedelta(seconds=6)).isoformat(), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "x",
             "content": [{"type": "text", "text": json.dumps(result)}]}]}})
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
    ref = f"e:{str(e.id)[:8]}"
    data = _stop(repo, f"Baseline {ref} was 1; mine got 1.2 but I did not record it.",
                 agent_transcript_path=str(_transcript(repo / "t.jsonl", started, worker.agent, {"id": ref})))
    assert process("subagent-stop", data, fired_at=(started + timedelta(minutes=1)).timestamp()).startswith("captured:")


def test_citation_must_be_by_this_runs_agent(repo: Path, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 3}, {"score": 1})
    started = datetime.now(UTC) - timedelta(minutes=5)
    ref = f"e:{str(e.id)[:8]}"
    other = _stop(repo, f"Recorded {ref}.",
                  agent_transcript_path=str(_transcript(repo / "o.jsonl", started, "worker-2", {"id": ref})))
    assert process("subagent-stop", other).startswith("captured:")
    mine = _stop(repo, f"Recorded {ref}.", agent_id="b",
                 agent_transcript_path=str(_transcript(repo / "m.jsonl", started, worker.agent, {"id": ref})))
    assert process("subagent-stop", mine) == "cited"


@pytest.mark.parametrize("receipt", ["missing", "read", "duplicate", "different_id"])
def test_citing_a_recent_peer_baseline_preserves_the_report(repo: Path, worker: Ledger, receipt: str) -> None:
    e = worker.record_experiment({"a": 4}, {"score": 1})
    ref = f"e:{str(e.id)[:8]}"
    started = datetime.now(UTC) - timedelta(minutes=5)
    result = {"id": ref}
    author: str | None = worker.agent
    tool = "assay_record_experiment"
    if receipt == "missing":
        author = None
        result = {}
    elif receipt == "read":
        tool = "assay_get"
    elif receipt == "duplicate":
        result["duplicate_of"] = ref
    elif receipt == "different_id":
        result["id"] = "e:00000000"
    path = _transcript(repo / "t.jsonl", started, author, result, tool)
    data = _stop(repo, f"Baseline {ref} scored 1; my new result is 1.2.", agent_transcript_path=str(path))

    assert process("subagent-stop", data).startswith("captured:")
    assert _notes(worker)[0]["payload"]["text"] == data["last_assistant_message"]


def test_citing_a_record_created_after_the_hook_is_captured(repo: Path, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 5}, {"score": 1})
    ref = f"e:{str(e.id)[:8]}"
    fired_at = datetime.now(UTC) - timedelta(minutes=5)
    path = _transcript(repo / "t.jsonl", fired_at - timedelta(minutes=1), worker.agent, {"id": ref})
    data = _stop(repo, f"See {ref}.", agent_transcript_path=str(path))

    assert process("subagent-stop", data, fired_at=fired_at.timestamp()).startswith("captured:")


def test_direct_cli_write_receipt_is_recognized(repo: Path, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 6}, {"score": 1})
    ref = f"e:{str(e.id)[:8]}"
    started = datetime.now(UTC) - timedelta(minutes=1)
    path = repo / "bash.jsonl"
    rows = [
        {"timestamp": started.isoformat(), "message": {"content": [
            {"type": "tool_use", "id": "bash", "name": "Bash",
             "input": {"command": f"ASSAY_AGENT={worker.agent} assay exp record --config '{{}}'"}}]}},
        {"timestamp": (started + timedelta(seconds=1)).isoformat(), "message": {"content": [
            {"type": "tool_result", "tool_use_id": "bash", "content": f"{ref} improved  ★ new best"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    data = _stop(repo, f"Recorded {ref}.", agent_transcript_path=str(path))

    assert process("subagent-stop", data) == "cited"


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


def test_replay_quarantines_invalid_utf8_and_drains_valid_reports(repo: Path, worker: Ledger,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    (spool / "broken.json").write_bytes(b"\xff")
    hooks._write_spool(spool, {"event": "subagent-stop", "project": worker.project_id,
                               "data": {"last_assistant_message": "Valid report."}})

    summary = hooks.replay_now(spool)

    assert (summary.captured, summary.dead) == (1, 1)
    assert list(spool.glob("*.json")) == []
    assert (spool / "broken.invalid").read_bytes() == b"\xff"
    assert _notes(worker)[0]["payload"]["text"] == "Valid report."


def test_replay_drains_arrivals_after_snapshot(repo: Path, worker: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    pending = hooks._pending
    queued = False

    def append_after_snapshot(path: Path) -> list[Path]:
        nonlocal queued
        snapshot = pending(path)
        if not queued:
            queued = True
            assert hooks._locked(spool)
            monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
                {"cwd": str(repo), "agent_type": "assay-worker", "last_assistant_message": "Concurrent report."})))
            hooks.main("subagent-stop")
        return snapshot

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("The foreground processor already owns the spool.")

    monkeypatch.setattr(hooks, "_pending", append_after_snapshot)
    monkeypatch.setattr(hooks.subprocess, "Popen", unexpected_spawn)

    summary = hooks.replay_now(spool)

    assert (summary.captured, summary.queued, summary.ok) == (1, 0, True)
    assert list(spool.glob("*.json")) == []
    assert _notes(worker)[0]["payload"]["text"] == "Concurrent report."


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
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path), "agent_type": "assay-worker",
                                                                     "last_assistant_message": "x"})))
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    hooks.main("subagent-stop")
    assert "no assay project" in (tmp_path / "state" / "assay" / "hook.log").read_text()


def test_other_subagents_exit_before_any_work(repo: Path, worker: Ledger) -> None:
    """The plugin hook fires for every subagent; a type outside ASSAY_HOOK_AGENTS records and spools nothing and
    returns quickly through the installed console script."""
    data = _stop(repo, "Explore results: the config lives in src/.", agent_type="Explore")
    state = repo / "state"
    env = {**os.environ, "XDG_STATE_HOME": str(state)}
    env.pop("ASSAY_HOOK_AGENTS", None)
    script = str(Path(sys.executable).parent / "assay")
    times = []
    for _ in range(3):
        t = time.monotonic()
        p = subprocess.run([script, "hook", "subagent-stop"], input=json.dumps(data), text=True,
                           capture_output=True, cwd=repo, timeout=30, env=env)
        times.append(time.monotonic() - t)
        assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
    assert min(times) < 0.5, f"hook took {min(times):.3f}s"
    assert not list(state.glob("assay/spool/*/*.json"))
    assert not (state / "assay" / "hook.log").exists()
    assert _notes(worker) == []


def test_plugin_hook_passes_every_subagent() -> None:
    root = Path(__file__).resolve().parent.parent / "integrations" / "claude-code"
    for f in (root / "hooks" / "hooks.json", root / "manual" / "settings.hooks.json"):
        [stop] = json.loads(f.read_text())["hooks"]["SubagentStop"]
        assert "matcher" not in stop, f


def test_untyped_events_are_not_captured_unless_wildcard(repo: Path, worker: Ledger,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook fires for every subagent: a new event without an agent type is dropped at the entry point, but an
    untyped entry already in the spool (written by an earlier build behind the plugin matcher) is still replayed."""
    import io

    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    monkeypatch.delenv("ASSAY_HOOK_AGENTS", raising=False)
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    spool = hooks._spool_dir()
    event = {"cwd": str(repo), "hook_event_name": "SubagentStop", "last_assistant_message": "Untyped report."}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    hooks.main("subagent-stop")
    assert not list(spool.glob("*.json"))
    monkeypatch.setenv("ASSAY_HOOK_AGENTS", "*")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    hooks.main("subagent-stop")
    assert len(list(spool.glob("*.json"))) == 1
    monkeypatch.delenv("ASSAY_HOOK_AGENTS")
    assert hooks.replay_now(spool).captured == 1
    assert _notes(worker)[0]["payload"]["text"] == "Untyped report."


def test_hook_help_is_not_read_as_an_event() -> None:
    script = str(Path(sys.executable).parent / "assay")
    p = subprocess.run([script, "hook", "--help"], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert p.returncode == 0 and "Usage" in p.stdout


def test_replay_keeps_the_capture_decision_made_at_enqueue(repo: Path, worker: Ledger,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom worker accepted under ASSAY_HOOK_AGENTS=implementer is still captured when `assay hook replay` runs
    from a shell with the default setting."""
    import io

    monkeypatch.setenv("XDG_STATE_HOME", str(repo / "state"))
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setenv("ASSAY_HOOK_AGENTS", "assay-worker,implementer")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"cwd": str(repo), "hook_event_name": "SubagentStop", "agent_type": "implementer",
         "last_assistant_message": "Implementer finding."})))
    hooks.main("subagent-stop")
    spool = hooks._spool_dir()
    [entry] = spool.glob("*.json")
    assert json.loads(entry.read_text())["accepted"] is True
    monkeypatch.delenv("ASSAY_HOOK_AGENTS")
    s = hooks.replay_now(spool)
    assert (s.captured, s.skipped) == (1, 0)
    assert _notes(worker)[0]["payload"]["text"] == "Implementer finding."
