"""The gaps a fourth review found in the third review's fixes (tests/test_third_review.py), each closed:

1. Raw JUnit hooks could accept stale or shared reports supplied by another invocation.
2. A piece created or renewed after waiting for the intent lock counted its lease from the transaction's start, so it
   could be over by the time the call returned.
3. A renewal without --paths that lost to `piece done` took the release for an expiry and started the piece again.
4. Entries queued under a token that never reached the ledger were silently left behind by a replay under the next.
5. An adoption retried after the original could not be removed copied, and recorded, the entry twice.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from benchbook import hooks, identity, pieces
from benchbook.errors import BenchbookError
from tests.test_check_hooks import fixture, hook, observed
from tests.test_check_run import SCRIPT, attempts, cli_env, make_checkout, spool_entries
from tests.test_review_holes import code_project, make_repo, owner_rows, tok
from tests.test_second_review import JUNIT_CONFIG, PASSING_XML, PY, _capture_entry
from tests.writer import as_writer

# ------------------------------------------------------------------------------------------- 1. before and after


def test_overlapping_raw_calls_cannot_share_junit_evidence(db_url: str, tmp_path: Path) -> None:
    from benchbook import Ledger
    from tests.conftest import make_project

    project = make_project(db_url, metric="checks_passed", direction="max", pack="code",
                           settings={"partition_key": "piece"})
    lg = Ledger.open(project=project, agent="lead", database_url=db_url)
    repo = make_checkout(tmp_path / "repo", project, cfg=JUNIT_CONFIG.format(project=project))
    env = cli_env(tmp_path, BENCHBOOK_DATABASE_URL=db_url)
    report = tmp_path / "report.xml"
    calls = [{**fixture("post_tool_use_bash.json", repo, f"{PY} -m pytest -q tests --junitxml={report}"),
              "tool_use_id": f"toolu_{i}"} for i in range(2)]
    for data in calls:  # previously installed manifests can still send the PreToolUse hook
        assert hook("bash-start", data, env, repo).returncode == 0
    report.write_text(PASSING_XML)  # only the second invocation produced the report
    procs = [subprocess.Popen([SCRIPT, "hook", "bash-result"], stdin=subprocess.PIPE, text=True, cwd=repo, env=env)
             for _ in calls]
    for proc, data in zip(procs, calls, strict=True):
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(data))
        proc.stdin.close()
    assert all(proc.wait(60) == 0 for proc in procs)
    events = observed(tmp_path)
    assert {e["tool_use_id"] for e in events} == {"toolu_0", "toolu_1"}
    assert all(e["kind"] == "unrecordable" and "benchbook check run unit" in e["reason"] for e in events)
    assert spool_entries(tmp_path / "state") == []
    assert attempts(lg) == []
    assert hook("task-completed", fixture("task_completed.json", repo), env, repo).returncode == 2


# ------------------------------------------------------------------------------------------- 2. leases after the lock


def _while_locked(db_url: str, project: str, hold_s: float, call: Any) -> Any:
    """Run call() in a thread while another session holds the intent lock; release it hold_s after the call's
    transaction began waiting. Returns what call() returned (or raised)."""
    got: dict[str, Any] = {}
    with psycopg.connect(db_url, autocommit=True) as locker:
        locker.execute("SELECT pg_advisory_lock(hashtext(%s))", ("intent:" + project,))

        def run() -> None:
            try:
                got["result"] = call()
            except Exception as e:  # noqa: BLE001
                got["error"] = e

        t = threading.Thread(target=run)
        t.start()
        deadline, began = time.monotonic() + 30, None
        while time.monotonic() < deadline and began is None:
            row = locker.execute("SELECT a.xact_start FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
                                 " WHERE l.locktype = 'advisory' AND NOT l.granted").fetchone()
            began = row[0] if row else None
            time.sleep(0.05)
        assert began is not None, "the call never waited for the intent lock"
        while locker.execute("SELECT clock_timestamp() < %s + make_interval(secs => %s)",
                             (began, hold_s)).fetchone()[0]:
            time.sleep(0.05)
        locker.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("intent:" + project,))
        t.join(30)
    if "error" in got:
        raise got["error"]
    return got["result"]


def _live(db_url: str, task_id: Any) -> bool:
    return bool(owner_rows(db_url, "SELECT lease_expires_at > clock_timestamp() AS live FROM benchbook.tasks"
                                   " WHERE id = %s", (task_id,))[0]["live"])


def test_a_new_piece_counts_its_lease_from_after_the_lock(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    started = _while_locked(db_url, project, 4, lambda: pieces.start(a, "media", ["src/media/*"], repo,
                                                                         lease_seconds=3))
    assert _live(db_url, started.task.id), "the 3 s lease was over when the call returned"


def test_a_renewal_counts_its_lease_from_after_the_lock(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    first = pieces.start(a, "media", ["src/media/*"], repo, lease_seconds=60)
    renewed = _while_locked(db_url, project, 4, lambda: pieces.start(a, "media", [], repo, lease_seconds=3))
    assert renewed.renewed and renewed.task.id == first.task.id and _live(db_url, first.task.id)


# ------------------------------------------------------------------------------------------- 3. renewal against done


def test_a_renewal_that_loses_to_done_does_not_restart_the_piece(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    pieces.start(a, "media", ["src/media/*"], repo)
    pieces.done(a, "media")
    with pytest.raises(BenchbookError, match="--paths"):
        pieces.start(a, "media", [], repo)
    assert not owner_rows(db_url, "SELECT id FROM benchbook.tasks WHERE project_id = %s AND status = 'claimed'",
                          (project,))


def test_a_renewal_that_finds_its_lease_ran_out_starts_again(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    held = pieces.start(a, "media", ["src/media/*"], repo).task
    owner_rows(db_url, "UPDATE benchbook.tasks SET lease_expires_at = now() - interval '1 second' WHERE id = %s"
                       " RETURNING id", (held.id,))
    again = pieces.start(a, "media", [], repo)
    assert not again.renewed and again.task.id != held.id and again.paths == ["src/media/*"]


# ------------------------------------------------------------------------------------------- 4 and 5. rotation


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_url: str) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    monkeypatch.delenv("BENCHBOOK_AGENT", raising=False)


def _spool(monkeypatch: pytest.MonkeyPatch, token: str) -> Path:
    monkeypatch.setenv("BENCHBOOK_TOKEN", token)
    spool = hooks._spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    return spool


def test_entries_of_a_token_that_never_reached_the_ledger_are_reported_as_held(
        db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    _, old = tok(db_url, project, "agent-a", "worker")
    _env(monkeypatch, tmp_path, db_url)
    spool_old = _spool(monkeypatch, old)
    hooks._write_spool(spool_old, _capture_entry(project, "queued; the ledger was never reached with this token"))
    new = identity.add_agent(project, "agent-a", rotate=True, database_url=db_url).token
    summary = hooks.replay_now(_spool(monkeypatch, new))
    assert (summary.processed, summary.held, summary.ok) == (0, 1, False)
    assert "held 1" in str(summary) and any(str(spool_old) in e for e in summary.errors)
    assert len(list(spool_old.glob("*.json"))) == 1


def test_a_retried_adoption_records_the_entry_once(db_url: str, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    _, old = tok(db_url, project, "agent-a", "worker")
    _env(monkeypatch, tmp_path, db_url)
    spool_old = _spool(monkeypatch, old)
    hooks._write_spool(spool_old, _capture_entry(project, "first, recorded under the old token"))
    assert hooks.replay_now(spool_old).captured == 1
    hooks._write_spool(spool_old, _capture_entry(project, "second, queued before the rotation"))
    [original] = list(spool_old.glob("*.json"))
    new = identity.add_agent(project, "agent-a", rotate=True, database_url=db_url).token
    spool_new = _spool(monkeypatch, new)

    real_unlink = Path.unlink

    def failing(self: Path, missing_ok: bool = False) -> None:
        if self == original:
            raise PermissionError("cannot remove")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing)
    first = hooks.replay_now(spool_new)
    assert (first.adopted, first.captured) == (1, 1) and original.exists()
    assert any("could not be removed" in e for e in first.errors)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    second = hooks.replay_now(spool_new)
    assert (second.adopted, second.captured, second.processed) == (0, 0, 0) and not original.exists()
    rows = owner_rows(db_url, "SELECT author FROM benchbook.claims WHERE project_id = %s", (project,))
    assert len(rows) == 2 and {r["author"] for r in rows} == {"agent-a"}
