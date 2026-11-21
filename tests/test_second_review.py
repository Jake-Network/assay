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

import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook import checks, hooks, pieces, pr, runner
from benchbook.check_hooks import junit_outcome, junit_report, match
from benchbook.errors import BenchbookError, Conflict, InvalidTransition, LeaseLost
from benchbook.runner import run_command
from tests.test_check_hooks import fixture, hook, observed
from tests.test_check_run import SCRIPT, cli_env, make_checkout, wait_attempts
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


def test_junit_report_is_read_from_the_command() -> None:
    assert junit_report("pytest --junitxml={junit}", [["pytest", "--junitxml=out/r.xml"]]) == "out/r.xml"
    assert junit_report("pytest --junitxml {junit}", [["pytest", "--junitxml", "r.xml"]]) == "r.xml"
    assert junit_report("pytest --junitxml={junit}", [["pytest"]]) is None


def test_a_junit_check_needs_a_fresh_passing_report(tmp_path: Path) -> None:
    (tmp_path / ".benchbook").mkdir()
    (tmp_path / ".benchbook" / "config.toml").write_text(JUNIT_CONFIG.format(project="p"))
    bare = match(f"{PY} -m pytest -q tests", tmp_path)
    assert bare is not None and bare.kind == "direct" and bare.junit and bare.report is None
    assert "wrote no JUnit report" in junit_outcome(bare, 1000)[2]
    found = match(f"{PY} -m pytest -q tests --junitxml=r.xml", tmp_path)
    assert found is not None and found.report == "r.xml"
    assert "no JUnit report at r.xml" in junit_outcome(found, 1000)[2]
    (tmp_path / "r.xml").write_text('<testsuite tests="0"></testsuite>')
    part, _, why = junit_outcome(found, 1000)
    assert part is not None and not why  # exit 0 and no test ran: a failure, as check run records it
    (tmp_path / "r.xml").write_text(PASSING_XML)
    assert junit_outcome(found, 1000) == (None, [], "")
    old = time.time() - 3600
    os.utime(tmp_path / "r.xml", (old, old))
    assert "predates this run" in junit_outcome(found, 1000)[2]


def test_the_gate_does_not_take_exit_0_of_a_junit_check_without_a_report(db_url: str, tmp_path: Path) -> None:
    from benchbook import Ledger
    from tests.conftest import make_project

    project = make_project(db_url, metric="checks_passed", direction="max", pack="code",
                           settings={"partition_key": "piece"})
    lg = Ledger.open(project=project, agent="lead", database_url=db_url)
    repo = make_checkout(tmp_path / "repo", project, cfg=JUNIT_CONFIG.format(project=project))
    env = cli_env(tmp_path, BENCHBOOK_DATABASE_URL=db_url)
    task = fixture("task_completed.json", repo)

    p = hook("bash-result", fixture("post_tool_use_bash.json", repo, f"{PY} -m pytest -q tests"), env, repo)
    assert (p.returncode, p.stderr) == (0, "")
    [seen] = observed(tmp_path)
    assert seen["kind"] == "unrecordable" and "no JUnit report" in seen["reason"]
    assert hook("task-completed", task, env, repo).returncode == 2

    report = tmp_path / "report.xml"  # outside the checkout, as the command would write it
    report.write_text(PASSING_XML)
    cmd = f"{PY} -m pytest -q tests --junitxml={report}"
    assert hook("bash-result", fixture("post_tool_use_bash.json", repo, cmd), env, repo).returncode == 0
    [row] = wait_attempts(lg, 1)
    assert row["metrics"]["checks"] == {"unit": "ok"}
    p = hook("task-completed", task, env, repo)
    assert (p.returncode, p.stderr) == (0, "")
    assert SCRIPT  # the hook ran as the installed command


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
