"""Code project status, recording warnings and the processor's statusline cache."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from benchbook import code_status, hooks
from benchbook.packs import load_pack
from tests.conftest import make_project
from tests.test_boundary_hooks import code_repo, context, fixture, run
from tests.test_code_brief import _verify, agent, attempt

CLI = [sys.executable, "-m", "benchbook"]


def cli(repo: Path, *args: str) -> str:
    result = subprocess.run([*CLI, *args], cwd=repo, capture_output=True, text=True, check=True, timeout=30)
    return result.stdout


@pytest.fixture
def project(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    defaults = load_pack("code").project_defaults
    name = make_project(db_url, metric=defaults["metric"], direction=defaults["direction"], pack="code",
                        settings=dict(defaults["settings"]))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_AGENT", "human")
    monkeypatch.setenv("BENCHBOOK_PROJECT", name)
    monkeypatch.delenv("BENCHBOOK_TOKEN", raising=False)
    return name


def test_status_counts_current_pieces_and_queue(db_url: str, project: str, tmp_path: Path) -> None:
    repo = code_repo(tmp_path / "repo", project)
    lg = agent(db_url, project, "impl-a")
    _verify(db_url, project, "verified", "1" * 40)
    _verify(db_url, project, "updated", "2" * 40)
    lg.propose_claim("completion", "Second report of the verified commit",
                     payload={"piece": "verified", "commit": "1" * 40})
    lg.propose_claim("completion", "New commit to check", payload={"piece": "updated", "commit": "3" * 40})
    failed = lg.propose_claim("completion", "Reported as passed", payload={"piece": "failed", "commit": "4" * 40})
    rerun = agent(db_url, project, "verifier", "harness")
    result = attempt(rerun, "failed", sha="4" * 40, failed=[["tests/test_piece.py::fails", "AssertionError"]])
    rerun.link(result.experiment_id, "refutes", failed.id)
    lg.propose_claim("observation", "Another module has a race", tags=["out-of-scope"])
    closed = lg.propose_claim("observation", "Another module has a typo", tags=["out-of-scope"])
    agent(db_url, project, "human", "human").decide(closed.id, "retracted", "human", "Fixed")
    spool = hooks._spool_dir()
    (spool / "dead").mkdir(parents=True)
    (spool / "pending.json").write_text("{}")
    (spool / "dead" / "failed.json").write_text("{}")
    hooks.log(f"hook processing failed for {spool}/pending.json")

    status = json.loads(cli(repo, "--json", "status"))
    assert status["project"] == project and status["pack"] == "code"
    assert status["pieces"] == {"verified": 1, "reported_only": 1, "rerun_mismatch": 1}
    assert status["open_findings"] == 1
    assert status["spool"] == {"pending": 1, "dead": 1, "stuck": 0}
    assert status["hook_failures_24h"] == 1
    text = cli(repo, "status")
    assert "1 verified, 1 reported only, 1 rerun mismatch" in text
    assert "1 pending, 1 dead" in text


def test_dead_queue_is_first_line_of_even_an_empty_brief(project: str, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo = code_repo(tmp_path / "repo", project)
    dead = hooks._spool_dir() / "dead"
    dead.mkdir(parents=True)
    (dead / "failed.json").write_text("{}")
    expected = "benchbook: recording stopped: 1 report(s) in dead/. Fix: benchbook hook replay"
    assert cli(repo, "brief").splitlines()[0] == expected
    out = run("session-start", fixture("session_start_startup.json", repo), capsys, monkeypatch)
    assert context(out).splitlines()[0] == expected


def test_pending_and_failure_are_scoped_to_the_active_spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://u@127.0.0.1:1/a")
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    waiting = spool / "waiting.json"
    waiting.write_text("{}")
    hooks.log("bash-result: queued unit fail (exit 1) for piece failed-build at 123abc")
    assert not code_status.recording_warning(tmp_path)
    old = time.time() - 600
    os.utime(waiting, (old, old))
    hooks.log(f"hook processing failed for {spool}/waiting.json")
    warning = code_status.recording_warning(tmp_path)
    assert "1 hook failure line(s) in the last 24h" in warning and "1 overdue report(s)" in warning
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://u@127.0.0.1:1/b")
    assert not code_status.recording_warning(tmp_path)


def test_statusline_reads_cache_written_by_the_processor(project: str, tmp_path: Path) -> None:
    repo = code_repo(tmp_path / "repo", project)
    assert cli(repo, "statusline") == ""
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    hooks._write_spool(spool, {"event": "subagent-stop", "project": project, "accepted": True, "data": {
        "agent_type": "benchbook-worker", "last_assistant_message": "Found a problem outside this piece."}})
    assert hooks.replay_now(spool).ok
    expected = "benchbook: verified 0/0, open findings 0"
    assert code_status.read_cache(project) == expected
    assert cli(repo, "statusline") == expected + "\n"
    assert cli(repo, "--project", project + "-other", "statusline") == ""
