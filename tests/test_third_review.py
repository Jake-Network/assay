"""The holes a third review reproduced in the fixes of the second (tests/test_second_review.py), each closed:

2. A heartbeat that began before its lease ran out committed after another piece had taken the paths: two live
   leases on the same paths.
3. A partial pass still linked "supports" to the completion, so it counted as a re-run in the false completion rate.
4. An expired piece that next_task had failed could not be started again without --paths.
5. After a token rotation, the agent's entries queued under the old token could not be replayed.
6. The matcher dropped the word before `--junitxml {junit}`, so the documented command never matched.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from benchbook.checks import verify as checks
from benchbook.code import pieces
from benchbook.core import identity
from benchbook.errors import BenchbookError, LeaseLost
from benchbook.hooks import spooler
from benchbook.hooks.checks import match
from tests.test_review_holes import code_project, commit, local, make_repo, owner_rows, tok
from tests.test_second_review import JUNIT_CONFIG, PY, TWO_CHECKS, _capture_entry
from tests.writer import as_writer

DOC = Path(__file__).resolve().parent.parent / "docs" / "code.md"


# ------------------------------------------------------------------------------------------- 2. heartbeat and the lock


def test_a_heartbeat_reads_the_expiry_after_the_intent_lock(db_url: str, tmp_path: Path) -> None:
    """The heartbeat's transaction starts while its lease is live and waits for the intent lock; meanwhile the lease
    runs out (and another piece could take the paths). Read after the lock, the lease is gone: LeaseLost."""
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    held = pieces.start(a, "media", ["src/media/*"], repo).task
    got: dict[str, Any] = {}
    with psycopg.connect(db_url, autocommit=True) as locker:
        locker.execute("SELECT pg_advisory_lock(hashtext(%s))", ("intent:" + project,))

        def beat() -> None:
            try:
                got["result"] = a.heartbeat(held.id)
            except Exception as e:  # noqa: BLE001
                got["error"] = e

        t = threading.Thread(target=beat)
        t.start()
        deadline = time.monotonic() + 30
        waiting = None
        while time.monotonic() < deadline:
            waiting = locker.execute(
                "SELECT a.xact_start FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
                " WHERE l.locktype = 'advisory' AND NOT l.granted").fetchone()
            if waiting:
                break
            time.sleep(0.05)
        assert waiting is not None, "the heartbeat never waited for the intent lock"
        # the lease runs out after the heartbeat's transaction began, before it gets the lock
        locker.execute("UPDATE benchbook.tasks SET lease_expires_at = %s + interval '1 millisecond' WHERE id = %s",
                       (waiting[0], held.id))
        while locker.execute("SELECT clock_timestamp() < %s + interval '1 second'", (waiting[0],)).fetchone()[0]:
            time.sleep(0.05)
        locker.execute("SELECT pg_advisory_unlock(hashtext(%s))", ("intent:" + project,))
        t.join(30)
    assert isinstance(got.get("error"), LeaseLost), got


# ------------------------------------------------------------------------------------------- 3. the numbers


def _numbers(db_url: str, project: str) -> dict[str, Any]:
    section = DOC.read_text().split("## The numbers", 1)[1].split("\n## ", 1)[0]
    [query] = re.findall(r"```sql\n(.*?)```", section, re.S)
    lead = local(db_url, project, "reader")
    [row] = lead.sql(query)["rows"]
    return row  # type: ignore[no-any-return]


def test_a_partial_pass_is_not_a_rerun_in_the_numbers(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    lead = local(db_url, project, "lead", "orchestrator")
    repo = make_repo(tmp_path / "repo", TWO_CHECKS)
    sha = commit(repo, "b-work", {"app.py": "VALUE = 1\n"})  # unit passes, other fails
    _, rec = checks.verify(lead, "media", sha, repo=repo, base="main", only=["unit"], reported_by="impl")
    assert rec.relation is None and rec.claim_id is not None
    assert not owner_rows(db_url, "SELECT id FROM benchbook.links WHERE dst_id = %s", (rec.claim_id,))
    n = _numbers(db_url, project)
    assert (n["completions"], n["rerun"], n["false_completions"], n["false_completion_rate"]) == (1, 0, 0, None)
    # a failure seen in a partial run still counts
    _, rec2 = checks.verify(lead, "media", sha, repo=repo, base="main", only=["other"], reported_by="impl")
    assert rec2.relation == "refutes"
    n = _numbers(db_url, project)
    assert (n["rerun"], n["false_completions"]) == (1, 1)


# ------------------------------------------------------------------------------------------- 4. restart after next_task


def test_a_piece_failed_by_next_task_starts_again_with_its_paths(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    lead, _ = tok(db_url, project, "lead")
    repo = make_repo(tmp_path / "repo")
    held = pieces.start(a, "media", ["src/media/*"], repo, tags=["mod:media"]).task
    owner_rows(db_url, "UPDATE benchbook.tasks SET lease_expires_at = now() - interval '1 second' WHERE id = %s"
                       " RETURNING id", (held.id,))
    assert lead.next_task() is None  # any agent's next_task fails the exhausted, expired intent
    assert lead.get_task(held.id).status == "failed"
    again = pieces.start(a, "media", [], repo)
    assert not again.renewed and again.paths == ["src/media/*"] and "mod:media" in again.task.tags


def test_a_released_piece_needs_its_paths_again(db_url: str, tmp_path: Path) -> None:
    project = code_project(db_url)
    a, _ = tok(db_url, project, "impl-a", "worker")
    repo = make_repo(tmp_path / "repo")
    pieces.start(a, "media", ["src/media/*"], repo)
    pieces.done(a, "media", failed=True)
    with pytest.raises(BenchbookError, match="declare the paths"):
        pieces.start(a, "media", [], repo)


# ------------------------------------------------------------------------------------------- 5. token rotation


def test_replay_with_a_rotated_token_adopts_the_same_agents_entries(db_url: str, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    _, old = tok(db_url, project, "agent-a", "worker")
    _, btok = tok(db_url, project, "agent-b", "worker")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    monkeypatch.delenv("BENCHBOOK_AGENT", raising=False)

    monkeypatch.setenv("BENCHBOOK_TOKEN", old)
    spool_old = spooler._spool_dir()
    spool_old.mkdir(parents=True)
    spooler._write_spool(spool_old, _capture_entry(project, "first report, recorded under the old token"))
    assert spooler.replay_now(spool_old).captured == 1  # the processor notes whose token this is
    assert json.loads((spool_old / spooler.WRITER_INFO).read_text())["agents"] == {project: "agent-a"}
    spooler._write_spool(spool_old, _capture_entry(project, "second report, queued while the ledger was down"))

    new = identity.add_agent(project, "agent-a", rotate=True, database_url=db_url).token
    monkeypatch.setenv("BENCHBOOK_TOKEN", btok)  # another agent adopts nothing
    spool_b = spooler._spool_dir()
    spool_b.mkdir(parents=True)
    assert spooler.replay_now(spool_b).adopted == 0 and len(list(spool_old.glob("*.json"))) == 1

    monkeypatch.setenv("BENCHBOOK_TOKEN", new)
    spool_new = spooler._spool_dir()
    spool_new.mkdir(parents=True)
    summary = spooler.replay_now(spool_new)
    assert (summary.adopted, summary.captured, summary.queued) == (1, 1, 0)
    assert not list(spool_old.glob("*.json"))
    rows = owner_rows(db_url, "SELECT author FROM benchbook.claims WHERE project_id = %s", (project,))
    assert [r["author"] for r in rows] == ["agent-a", "agent-a"]


def test_entries_no_processor_confirmed_stay_where_they_are(db_url: str, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    _, old = tok(db_url, project, "agent-a", "worker")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    monkeypatch.setenv("BENCHBOOK_TOKEN", old)
    spool_old = spooler._spool_dir()
    spool_old.mkdir(parents=True)
    spooler._write_spool(spool_old, _capture_entry(project, "queued, never processed"))
    # someone else's note on the target but naming no agent in this project
    (spool_old / spooler.WRITER_INFO).write_text(json.dumps({"target": spooler._target_key(),
                                                           "writer": spooler.writer_identity(), "agents": {}}))
    new = identity.add_agent(project, "agent-a", rotate=True, database_url=db_url).token
    monkeypatch.setenv("BENCHBOOK_TOKEN", new)
    spool_new = spooler._spool_dir()
    spool_new.mkdir(parents=True)
    summary = spooler.replay_now(spool_new)
    assert summary.adopted == 0 and len(list(spool_old.glob("*.json"))) == 1
    assert summary.held == 1 and not summary.ok
    assert any("no processor ever confirmed" in e for e in summary.errors)


# ------------------------------------------------------------------------------------------- 6. --junitxml {junit}


def test_a_bare_junit_word_keeps_the_words_before_it(tmp_path: Path) -> None:
    (tmp_path / ".benchbook").mkdir()
    cfg = JUNIT_CONFIG.format(project="p").replace("--junitxml={junit}", "--junitxml {junit}")
    (tmp_path / ".benchbook" / "config.toml").write_text(cfg)
    found = match(f"{PY} -m pytest -q tests --junitxml out/r.xml", tmp_path)
    assert found is not None and found.kind == "direct" and found.check == "unit" and found.junit
    bare = match(f"{PY} -m pytest -q tests", tmp_path)
    assert bare is not None and bare.kind == "direct" and bare.check == "unit" and bare.junit
    assert match(f"{PY} -m pytest -q --junitxml out/r.xml", tmp_path) is None  # "tests" is part of the check
