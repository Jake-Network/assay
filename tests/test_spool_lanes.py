"""The spool's two lanes (B2 step 2b): re-runs in one, every other entry in the other, each with its own processor
and lock, so a worker's check run is recorded while re-runs are queued; and nothing queued before a re-run is
recorded after it (the verify lane settles the records lane first)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchbook.checks import attempts, verify_requests
from benchbook.hooks import spooler
from tests.conftest import CONFIG, make_project, make_repo, piece

REAL_START = spooler.start_processor


@pytest.fixture
def setup(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from benchbook.packs import load_pack

    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("BENCHBOOK_PROJECT", "BENCHBOOK_TOKEN", "BENCHBOOK_AGENT"):
        monkeypatch.delenv(name, raising=False)
    d = load_pack("code").project_defaults
    project = make_project(db_url, metric=d["metric"], direction=d["direction"], pack="code",
                           settings=dict(d["settings"]))
    repo = make_repo(tmp_path / "repo", CONFIG.replace('project = "ignored"', f'project = "{project}"'))
    sha = piece(repo, "media-upload", {"impl.txt": "fixed\n"})
    started: list[str] = []
    monkeypatch.setattr(spooler, "start_processor", lambda spool, lane=spooler.RECORDS: started.append(lane))
    spool = spooler._spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    return {"project": project, "repo": repo, "sha": sha, "spool": spool, "started": started}


def _queue_verify(s: dict[str, Any]) -> None:
    line = verify_requests.enqueue({
        "session_id": "s-lanes", "cwd": str(s["repo"]), "hook_event_name": "PostToolUse", "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose"},
        "tool_response": {"status": "completed", "content": [{"type": "text",
                                                               "text": f"piece: media-upload\nDONE {s['sha']}"}]}})
    assert line.startswith("benchbook: re-running the checks of media-upload")


def _queue_check_run(s: dict[str, Any]) -> Path:
    before = set(s["spool"].glob("*.json"))
    spooler._write_spool(s["spool"], {"event": "check-run", "project": s["project"], "accepted": True, "data": {
        "check": "unit", "piece": "media-upload", "status": "ok", "sha": s["sha"], "exit_code": 0,
        "repo": str(s["repo"])}})
    [path] = set(s["spool"].glob("*.json")) - before
    return path


def _events(spool: Path) -> list[str]:
    return sorted(json.loads(p.read_text())["event"] for p in spool.glob("*.json"))


def test_a_check_run_is_recorded_while_a_re_run_holds_the_verify_lane(setup: dict[str, Any]) -> None:
    spool = setup["spool"]
    _queue_verify(setup)
    assert setup["started"] == [spooler.VERIFY]
    _queue_check_run(setup)
    with spooler._lock(spool, blocking=True, lane=spooler.VERIFY):  # a re-run is running
        assert not spooler._locked(spool, spooler.RECORDS)
        spooler.replay(spool, spooler.RECORDS)
        assert _events(spool) == ["verify-request"]  # the attempt is in the ledger, the re-run still queued
    spooler.replay(spool, spooler.VERIFY)
    assert _events(spool) == []
    notes = verify_requests.session_notes.notes_dir("s-lanes")
    assert notes is not None and any(p.name.startswith("verify-") for p in notes.iterdir())


def test_the_verify_lane_records_what_was_queued_before_a_re_run_first(setup: dict[str, Any],
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """With one processor an attempt queued before a report was in the ledger when its re-run ran (the re-run reads
    the implementers' passes, T03): the verify lane keeps that order by settling the records lane first."""
    order: list[str] = []
    record, process = attempts.record_spooled, verify_requests.process
    monkeypatch.setattr(attempts, "record_spooled", lambda *a, **k: order.append("attempt") or record(*a, **k))
    monkeypatch.setattr(verify_requests, "process", lambda *a, **k: order.append("re-run") or process(*a, **k))
    _queue_check_run(setup)
    _queue_verify(setup)
    spooler.replay(setup["spool"], spooler.VERIFY)
    assert order == ["attempt", "re-run"] and _events(setup["spool"]) == []


def test_each_lane_starts_its_own_processor(setup: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    spool, launched = setup["spool"], []
    monkeypatch.setattr(spooler, "start_processor", REAL_START)  # the real one, with Popen recorded
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: launched.append(args[-2:]))
    with spooler._lock(spool, blocking=True, lane=spooler.RECORDS):
        spooler.start_processor(spool)  # the records processor is running: nothing to start
        spooler.start_processor(spool, spooler.VERIFY)
    with spooler._lock(spool, blocking=True, lane=spooler.VERIFY):
        spooler.start_processor(spool, spooler.VERIFY)
        spooler.start_processor(spool)
    assert launched == [[str(spool), spooler.VERIFY], [str(spool), spooler.RECORDS]]


def test_an_attempt_queued_while_a_re_run_settles_gets_a_processor(setup: dict[str, Any],
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """An attempt written while the verify lane holds the records lock (settling) finds that lock held and starts
    nothing; the verify lane starts the records processor for it after settling."""
    _queue_verify(setup)
    real, arrived = spooler._drain, []

    def drain_then_arrive(spool: Path, delays: tuple[float, ...], summary: Any, attempted: Any = (),
                          lane: str | None = None) -> set[str]:
        out = real(spool, delays, summary, attempted, lane)
        if lane == spooler.RECORDS and not arrived:
            arrived.append(_queue_check_run(setup))  # its producer saw the records lock held
        return out

    monkeypatch.setattr(spooler, "_drain", drain_then_arrive)
    setup["started"].clear()
    spooler.replay(setup["spool"], spooler.VERIFY)
    assert arrived and setup["started"] == [spooler.RECORDS] and _events(setup["spool"]) == ["check-run"]


def test_a_records_processor_starts_a_waiting_re_run(setup: dict[str, Any]) -> None:
    """A re-run left in the spool (a stopped processor, an older version) is started by the next processor of the
    other lane, as one processor used to take everything."""
    _queue_verify(setup)
    _queue_check_run(setup)
    setup["started"].clear()
    spooler.replay(setup["spool"], spooler.RECORDS)
    assert setup["started"] == [spooler.VERIFY] and _events(setup["spool"]) == ["verify-request"]


def test_the_real_processors_drain_a_left_re_run_and_a_new_attempt(setup: dict[str, Any]) -> None:
    import time

    _queue_verify(setup)  # left: no processor was started for it
    _queue_check_run(setup)
    REAL_START(setup["spool"])  # what the Bash hook or check run does: the records lane only
    deadline = time.monotonic() + 120
    while list(setup["spool"].glob("*.json")) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert _events(setup["spool"]) == []


def test_replay_now_takes_both_lanes(setup: dict[str, Any]) -> None:
    _queue_check_run(setup)
    _queue_verify(setup)
    summary = spooler.replay_now(setup["spool"])
    assert summary.recorded == 2 and _events(setup["spool"]) == []
