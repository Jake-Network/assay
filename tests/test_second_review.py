"""The holes a second review of the code pack reproduced, each closed (docs/code.md, docs/claude-code.md):

1. A partial check verify (--checks) was recorded as an ordinary passing run, and the all_pass rule and the pull
   request summary took it for a verification although a declared check it skipped fails.
2. Two agents sharing a database URL and a state directory with different tokens shared one spool: one agent's
   replay recorded the other's queued entries in its own name.
3. The Bash hook recorded a junit = true check that exited 0 as passed without any JUnit report, and the completion
   gate let the task close on it.
4. A heartbeat revived an expired lease, so an expired piece and the piece that took its paths were both live.
5. CI found the completion claim by tags and payload, check verify by two fixed statements: a report written in
   other words got a second claim, and the re-run's evidence went there.
6. After a timeout the runner waited for the shell only: a child that ignored SIGTERM outlived the run.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook.checks import runner
from benchbook.checks import verify as checks
from benchbook.checks.runner import run_command
from benchbook.code import pieces, pr
from benchbook.errors import BenchbookError, Conflict, InvalidTransition, LeaseLost
from benchbook.hooks import subagent as hooks
from tests.test_check_hooks import fixture, hook, observed, start_piece
from tests.test_check_run import SCRIPT, cli_env, git, make_checkout, wait_attempts
from tests.test_review_holes import code_project, commit, local, make_repo, owner_rows, tok
from tests.writer import as_writer

PY = shlex.quote(sys.executable)
TWO_CHECKS = f'''project = "ignored"

[checks.unit]
cmd = "{PY} check.py"

[checks.other]
cmd = "{PY} -c 'import app, sys; sys.exit(0 if getattr(app, \\"OTHER\\", 0) else 1)'"

[guard]
paths = ["tests/**", "**/conftest.py", ".benchbook/config.toml", "check.py"]
base = "main"
'''
PASSING_XML = '<testsuite><testcase classname="t" name="a"/></testsuite>'


# ------------------------------------------------------------------------------------------- 1. partial runs


def test_a_partial_verify_is_recorded_and_never_verifies(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    lead = local(db_url, project, "lead", "orchestrator")
    repo = make_repo(tmp_path / "repo", TWO_CHECKS)
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\n"})  # unit passes, other fails

    v, rec = checks.verify(lead, "media", sha, repo=repo, base="main", only=["unit"], reported_by="impl")
    assert v.all_passed and not v.complete and v.missing == ["other"]
    exp = owner_rows(db_url, "SELECT metrics, notes FROM benchbook.experiments WHERE id = %s", (rec.experiment_id,))[0]
    assert exp["metrics"]["checks_declared"] == ["unit", "other"] and exp["metrics"]["checks_total"] == 1
    assert "partial: 1 of 2 declared checks run" in exp["notes"]
    assert rec.outcome is not None and rec.outcome.decision is None
    assert "only a run of every declared check can verify" in rec.outcome.reason
    assert lead.completion_attempts(rec.claim_id)["status"] == "proposed"  # type: ignore[index]
    with pytest.raises(InvalidTransition, match="every declared check"):
        lead.check_rerun(rec.claim_id, [rec.experiment_id])  # type: ignore[arg-type]
    [state] = pr.describe(lead, [pr.Piece("media", sha)])
    assert state.state == pr.CLAIMED and state.partial == 1 and state.independent == []

    # the full run fails on the check the partial one left out
    v2, rec2 = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl")
    assert v2.complete and not v2.all_passed and rec2.relation == "refutes"
    assert rec2.outcome is not None and rec2.outcome.decision is None  # not inconclusive: nothing disagrees


def test_a_full_verify_after_a_partial_one_verifies(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    lead = local(db_url, project, "lead", "orchestrator")
    repo = make_repo(tmp_path / "repo", TWO_CHECKS)
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\nOTHER = 1\n"})
    checks.verify(lead, "media", sha, repo=repo, base="main", only=["other"], reported_by="impl")
    _, rec = checks.verify(lead, "media", sha, repo=repo, base="main", reported_by="impl")
    assert rec.outcome is not None and rec.outcome.decision == "verified"
    [state] = pr.describe(lead, [pr.Piece("media", sha)])
    assert state.state == pr.VERIFIED


# ------------------------------------------------------------------------------------------- 2. the spool's writer


def _capture_entry(project: str, text: str) -> dict[str, Any]:
    return {"event": "subagent-stop", "project": project, "spooled_at": time.time(), "accepted": True,
            "data": {"hook_event_name": "SubagentStop", "agent_type": "benchbook-worker",
                     "last_assistant_message": text}}


def test_an_entry_is_recorded_only_under_the_token_it_was_queued_with(db_url: str, tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    _, atok = tok(db_url, project, "agent-a", "worker")
    _, btok = tok(db_url, project, "agent-b", "worker")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    monkeypatch.delenv("BENCHBOOK_AGENT", raising=False)

    monkeypatch.delenv("BENCHBOOK_TOKEN", raising=False)
    plain = hooks._spool_dir()
    monkeypatch.setenv("BENCHBOOK_TOKEN", atok)
    spool_a = hooks._spool_dir()
    spool_a.mkdir(parents=True)
    hooks._write_spool(spool_a, _capture_entry(project, "report of agent a, queued with its token"))
    [path] = list(spool_a.glob("*.json"))

    monkeypatch.setenv("BENCHBOOK_TOKEN", btok)
    spool_b = hooks._spool_dir()
    assert len({plain, spool_a, spool_b}) == 3  # one spool per token on the same database
    spool_b.mkdir(parents=True)
    summary = hooks.replay_now(spool_b)
    assert summary.processed == 0 and path.exists()
    # the entry itself is bound too: handed to B's processor, it stays queued
    assert hooks.process_file(path).startswith("held:queued under another BENCHBOOK_TOKEN") and path.exists()
    assert not owner_rows(db_url, "SELECT id FROM benchbook.claims WHERE project_id = %s", (project,))

    monkeypatch.setenv("BENCHBOOK_TOKEN", atok)
    assert hooks.replay_now(spool_a).captured == 1 and not path.exists()
    [row] = owner_rows(db_url, "SELECT author FROM benchbook.claims WHERE project_id = %s", (project,))
    assert row["author"] == "agent-a"


def test_without_a_token_the_spool_is_where_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Entries queued before entries were bound (no writer field) stay in the same spool and are recorded there."""
    import hashlib
    import json

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://example/db")
    monkeypatch.delenv("BENCHBOOK_TOKEN", raising=False)
    key = hashlib.sha256(json.dumps(["postgresql://example/db"]).encode()).hexdigest()
    assert hooks._spool_dir().name == key and hooks.writer_identity() is None
    monkeypatch.setenv("BENCHBOOK_TOKEN", "bbt_x")
    ident = hooks.writer_identity()
    assert ident is not None and "bbt_x" not in ident and hooks._spool_dir().name != key


# ------------------------------------------------------------------------------------------- 3. the Bash hook and JUnit

JUNIT_CONFIG = f'''project = "{{project}}"

[checks.unit]
cmd = "{PY} -m pytest -q tests --junitxml={{{{junit}}}}"
junit = true

[guard]
base = "main"
'''


def test_the_gate_requires_wrapper_evidence_for_junit(db_url: str, tmp_path: Path) -> None:
    from benchbook import Ledger
    from tests.conftest import make_project

    project = make_project(db_url, metric="checks_passed", direction="max", pack="code",
                           settings={"partition_key": "piece"})
    lg = Ledger.open(project=project, agent="lead", database_url=db_url)
    repo = make_checkout(tmp_path / "repo", project, cfg=JUNIT_CONFIG.format(project=project))
    start_piece(db_url, project, repo)
    env = cli_env(tmp_path, BENCHBOOK_DATABASE_URL=db_url)
    task = fixture("task_completed.json", repo)

    p = hook("bash-result", fixture("post_tool_use_bash.json", repo, f"{PY} -m pytest -q tests"), env, repo)
    assert (p.returncode, p.stderr) == (0, "")
    [seen] = observed(tmp_path)
    assert seen["kind"] == "unrecordable" and "benchbook check run unit" in seen["reason"]
    assert hook("task-completed", task, env, repo).returncode == 2
    failed = fixture("post_tool_use_failure_bash.json", repo, f"{PY} -m pytest -q tests")
    assert hook("bash-result", failed, env, repo).returncode == 0
    [row] = wait_attempts(lg, 1)
    assert row["metrics"]["checks"] == {"unit": "fail"} and row["metrics"]["via"] == "bash-hook"
    assert hook("task-completed", task, env, repo).returncode == 2

    p = subprocess.run([SCRIPT, "check", "run", "unit"], cwd=repo, env=env, capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr
    rows = wait_attempts(lg, 2)
    assert len(rows) == 2
    [wrapped] = [row for row in rows if row["metrics"]["via"] == "check-run"]
    assert wrapped["metrics"]["checks"] == {"unit": "ok"}
    p = hook("task-completed", task, env, repo)
    assert (p.returncode, p.stderr) == (0, "")


def test_older_raw_junit_passes_do_not_open_the_gate(db_url: str, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    lg = local(db_url, project, "lead", "orchestrator")
    repo = make_checkout(tmp_path / "repo", project, cfg=JUNIT_CONFIG.format(project=project))
    start_piece(db_url, project, repo)
    sha = git(repo, "rev-parse", "HEAD")
    env = cli_env(tmp_path, BENCHBOOK_DATABASE_URL=db_url)
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    hooks._write_spool(spool, {
        "event": "bash-result", "project": project, "accepted": True,
        "data": {"piece": "media", "check": "unit", "sha": sha, "status": "ok", "dirty": False, "repo": str(repo)},
    })

    task = fixture("task_completed.json", repo)
    assert hook("task-completed", task, env, repo).returncode == 2
    assert hooks.replay_now(spool).recorded == 1
    [recorded] = wait_attempts(lg, 1)
    assert recorded["metrics"]["checks"] == {"unit": "ok"} and recorded["metrics"]["via"] == "bash-hook"
    assert hook("task-completed", task, env, repo).returncode == 2


# ------------------------------------------------------------------------------------------- 4. heartbeat


def test_a_heartbeat_does_not_revive_an_expired_piece(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    b, _ = tok(db_url, project, "impl-b", "worker")
    repo = make_repo(tmp_path / "repo")
    held = pieces.start(a, "media", ["src/media/*"], repo).task
    owner_rows(db_url, "UPDATE benchbook.tasks SET lease_expires_at = now() - interval '1 second' WHERE id = %s"
                       " RETURNING id", (held.id,))
    pieces.start(b, "upload", ["src/media/*"], repo)  # the paths are free once the lease ran out

    with pytest.raises(LeaseLost):
        a.heartbeat(held.id)
    with pytest.raises(LeaseLost):
        a.update_intent(held.id, {}, lease_seconds=600)
    with pytest.raises(Conflict, match="already holds these paths"):  # starting again goes through the check
        pieces.start(a, "media", [], repo)
    live = owner_rows(db_url, "SELECT lease_owner FROM benchbook.tasks WHERE project_id = %s AND status = 'claimed'"
                              " AND lease_expires_at > now()", (project,))
    assert [r["lease_owner"] for r in live] == ["impl-b"]


def test_an_expired_piece_starts_again_with_its_paths_when_they_are_free(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    held = pieces.start(a, "media", ["src/media/*"], repo, tags=["mod:media"]).task
    owner_rows(db_url, "UPDATE benchbook.tasks SET lease_expires_at = now() - interval '1 second' WHERE id = %s"
                       " RETURNING id", (held.id,))
    again = pieces.start(a, "media", [], repo)
    assert not again.renewed and again.task.id != held.id and again.paths == ["src/media/*"]
    assert "mod:media" in again.task.tags


# ------------------------------------------------------------------------------------------- 5. the CI's claim


def test_ci_links_its_rerun_to_the_claim_the_summary_shows(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    impl = local(db_url, project, "impl", "worker")
    ci = local(db_url, project, "ci:41", "harness")
    repo = make_repo(tmp_path / "repo")
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\n"})
    other = commit(repo, "b-other", {"app.py": "VALUE = 1  # other\n"})
    claim = impl.propose_claim("completion", "Done: media upload no longer stores a file twice",
                               payload={"piece": "media", "commit": sha}, tags=["piece:media", f"commit:{sha}"]).id
    found = pr.completion(ci, pr.Piece("media", sha))
    assert found == (claim, "impl")

    _, rec = checks.verify(ci, "media", sha, repo=repo, base="main", claim=str(claim), decide=False)
    assert rec.claim_id == claim and rec.claim_existed and rec.relation == "supports" and rec.reported_by == "impl"
    # --reported-by alone finds the same claim too: its statement does not matter
    _, rec2 = checks.verify(ci, "media", sha, repo=repo, base="main", reported_by="impl", decide=False)
    assert rec2.claim_id == claim and rec2.claim_existed
    assert len(owner_rows(db_url, "SELECT id FROM benchbook.claims WHERE project_id = %s AND kind = 'completion'",
                          (project,))) == 1

    with pytest.raises(BenchbookError, match="not media at"):
        checks.verify(ci, "media", other, repo=repo, base="main", claim=str(claim), decide=False)
    with pytest.raises(BenchbookError, match="records the report of impl"):
        checks.verify(ci, "media", sha, repo=repo, base="main", claim=str(claim), reported_by="someone",
                      decide=False)
    with pytest.raises(BenchbookError, match="cannot verify its own report"):
        checks.verify(impl, "media", sha, repo=repo, base="main", claim=str(claim), decide=False)


# ------------------------------------------------------------------------------------------- 6. the runner's cleanup

IGNORES_TERM = """import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open("child", "w").write(str(os.getpid()))
print("started", flush=True)
time.sleep(120)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and process groups")
@pytest.mark.parametrize("tee", [False, True])
def test_a_child_that_ignores_sigterm_does_not_outlive_the_timeout(tmp_path: Path, tee: bool,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_runner import _gone, _py, _wait_gone

    monkeypatch.setattr(runner, "_KILL_GRACE_S", 1.0)
    cmd = _py(tmp_path, "ignore.py", IGNORES_TERM) + " & wait"
    res = run_command(cmd, tmp_path, tmp_path / "run.log", 3, tee=tee)
    assert res.run_status == "timeout"
    child = int((tmp_path / "child").read_text())
    assert _wait_gone(child, within=2.0), "the child that ignored SIGTERM is still running"
    assert _gone(child)
