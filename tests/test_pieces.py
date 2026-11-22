"""Path occupancy (PR 2-P1b): `benchbook piece start <piece> --paths 'glob'...` declares the paths a piece will change
as a worker intent, and a piece whose paths overlap a live one's is refused with the first piece's task id."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest

from benchbook import Ledger, pieces
from benchbook.errors import BenchbookError, Conflict, PermissionDenied, ValidationFailed
from benchbook.packs import load_pack
from tests.conftest import make_project
from tests.writer import token_ledger

DOC = Path(__file__).resolve().parent.parent / "docs" / "code.md"
FILES = ["src/media/upload.py", "src/media/store.py", "src/auth/login.py", "app/[id]/page.tsx", "README.md"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: list[str]) -> None:
    for f in files:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(f"# {f}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "files")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    commit(root, FILES)
    return root


@pytest.fixture
def code(db_url: str) -> str:
    """A project made the way `benchbook init --pack code` makes one: its defaults let workers start pieces."""
    d = load_pack("code").project_defaults
    return make_project(db_url, metric=d["metric"], direction=d["direction"], pack="code",
                        settings=dict(d["settings"]))


def agent(db_url: str, project: str, name: str, role: str = "worker") -> Ledger:
    return Ledger.open(project=project, agent=name, role=role, database_url=db_url)


def run_out(db_url: str, task: object) -> None:
    """End a lease on the owner's connection (no sleeping, so a slow machine cannot make this flaky)."""
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE benchbook.tasks SET lease_expires_at = clock_timestamp() - interval '1 second'"
                     " WHERE id = %s", (task,))


def test_same_file_is_refused_with_the_first_pieces_id(db_url: str, code: str, repo: Path) -> None:
    a, b = agent(db_url, code, "a"), agent(db_url, code, "b")
    first = pieces.start(a, "media-upload", ["src/media/upload.py"], repo)
    assert not first.renewed and first.paths == ["src/media/upload.py"] and first.new == []
    t = first.task
    assert (t.status, t.lease_owner, t.created_by) == ("claimed", "a", "a")
    assert set(t.tags) == {"intent", "piece", "piece:media-upload"} and t.title == "piece media-upload"
    assert t.spec == {"piece": "media-upload", "paths": ["src/media/upload.py"], "declared": ["src/media/upload.py"]}
    with pytest.raises(Conflict) as err:
        pieces.start(b, "media-dedupe", ["src/media/upload.py", "src/auth/login.py"], repo)
    assert t.short_id in str(err.value) and "piece media-upload" in str(err.value)
    assert err.value.agent == "a" and err.value.existing == t.id
    assert b.my_intents() == []  # nothing was written for the refused piece
    pieces.start(b, "auth", ["src/auth/login.py"], repo)  # paths that do not overlap are fine


def test_overlapping_globs(db_url: str, code: str, repo: Path) -> None:
    a, b, c = agent(db_url, code, "a"), agent(db_url, code, "b"), agent(db_url, code, "c")
    s = pieces.start(a, "media", ["src/media/*"], repo)
    assert s.paths == ["src/media/store.py", "src/media/upload.py"]  # expanded against git ls-files
    with pytest.raises(Conflict, match=s.task.short_id):
        pieces.start(b, "uploads", ["src/**/upload.py"], repo)  # glob vs the files it expanded to
    n = pieces.start(b, "billing", ["src/billing/**"], repo)  # no tracked file: kept as a glob
    assert n.paths == ["src/billing/**"] and n.new == ["src/billing/**"]
    with pytest.raises(Conflict, match=n.task.short_id) as err:
        pieces.start(c, "invoices", ["src/*/invoice.py"], repo)  # glob vs glob, neither matching a file yet
    assert "both match src/billing/invoice.py" in str(err.value)
    pieces.start(c, "docs", ["docs/**", "README.md"], repo)


def test_new_file_glob_against_an_existing_file(db_url: str, code: str, repo: Path) -> None:
    a, b = agent(db_url, code, "a"), agent(db_url, code, "b")
    s = pieces.start(a, "billing", ["src/billing/*.py"], repo)  # the piece will add these files
    assert s.paths == ["src/billing/*.py"]
    commit(repo, ["src/billing/api.py"])  # meanwhile the file lands on the branch
    with pytest.raises(Conflict, match=s.task.short_id):
        pieces.start(b, "api", ["src/billing/api.py"], repo)  # now an existing file, under the first piece's glob
    # a file whose name holds [ ] is that one file, not a character class
    r = pieces.start(b, "route", ["app/[id]/page.tsx"], repo)
    assert r.paths == ["app/\\[id]/page.tsx"] and r.new == []
    with pytest.raises(Conflict, match=r.task.short_id):
        pieces.start(a, "pages", ["app/*/page.tsx"], repo)
    pieces.start(a, "x-route", ["app/[x]/page.tsx"], repo)  # a glob for app/x/page.tsx: no overlap


def test_an_expired_lease_frees_the_paths(db_url: str, code: str, repo: Path) -> None:
    a, b = agent(db_url, code, "a"), agent(db_url, code, "b")
    s = pieces.start(a, "media", ["src/media/*"], repo)
    with pytest.raises(Conflict):
        pieces.start(b, "store", ["src/media/store.py"], repo)
    run_out(db_url, s.task.id)
    assert [r["piece"] for r in pieces.held(a)] == []
    assert pieces.start(b, "store", ["src/media/store.py"], repo).task.lease_owner == "b"


def test_two_concurrent_starts_exactly_one_wins(db_url: str, code: str, repo: Path) -> None:
    """Both starts block on the intent lock held here; neither has checked anything the other could miss. If the
    path check ran outside the lock, both would pass it and both would win."""
    a, b = agent(db_url, code, "a"), agent(db_url, code, "b")
    results: dict[str, object] = {}

    def go(lg: Ledger, piece: str) -> None:
        try:
            results[piece] = pieces.start(lg, piece, ["src/media/upload.py"], repo)
        except Exception as e:  # noqa: BLE001 - the test reads what each thread got
            results[piece] = e

    with psycopg.connect(db_url) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("intent:" + code,))
        threads = [threading.Thread(target=go, args=(lg, name)) for lg, name in ((a, "one"), (b, "two"))]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 30
        with psycopg.connect(db_url, autocommit=True) as probe:
            while probe.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                                ).fetchone()[0] < 2:  # type: ignore[index]
                assert time.monotonic() < deadline, "the two starts never queued on the lock"
                time.sleep(0.02)
        holder.commit()
    for t in threads:
        t.join(30)
    won = [k for k, v in results.items() if isinstance(v, pieces.Started)]
    lost = [v for v in results.values() if isinstance(v, Conflict)]
    assert len(won) == 1 and len(lost) == 1, results
    assert results[won[0]].task.short_id in str(lost[0])  # type: ignore[attr-defined]


def test_the_same_agent_restarting_its_piece(db_url: str, code: str, repo: Path) -> None:
    a, b = agent(db_url, code, "a"), agent(db_url, code, "b")
    first = pieces.start(a, "media", ["src/media/upload.py"], repo, lease_seconds=60)
    again = pieces.start(a, "media", ["src/media/store.py"], repo, lease_seconds=60)  # new paths replace the old
    assert again.renewed and again.task.id == first.task.id and again.paths == ["src/media/store.py"]
    assert pieces.start(b, "upload", ["src/media/upload.py"], repo).task.lease_owner == "b"  # freed
    renew = pieces.start(a, "media", [], repo, lease_seconds=3600)  # no --paths: a heartbeat that keeps them
    assert renew.renewed and renew.paths == ["src/media/store.py"]
    assert renew.task.lease_expires_at - again.task.lease_expires_at > timedelta(minutes=30)  # type: ignore[operator]
    assert [t.id for t in a.my_intents()] == [first.task.id]  # still one task
    with pytest.raises(Conflict, match="already working on this \\(piece\\)"):
        pieces.start(b, "media", ["docs/**"], repo)  # someone else's piece, whatever the paths
    with pytest.raises(Conflict, match=first.task.short_id):
        pieces.start(a, "other", ["src/media/store.py"], repo)  # the same agent's other piece may not overlap either
    with pytest.raises(Conflict, match="upload"):
        pieces.start(a, "media", ["src/media/*"], repo)  # a restart is checked against the others too
    assert pieces.held(a)[0]["paths"] == ["src/media/store.py"]  # and the refused restart changed nothing
    with pytest.raises(BenchbookError, match="--paths"):
        pieces.start(a, "fresh", [], repo)  # a first start must declare its paths


@pytest.mark.parametrize("start_first", [True, False])
def test_restarting_an_expired_piece_serializes_with_release(
        db_url: str, code: str, repo: Path, monkeypatch: pytest.MonkeyPatch, start_first: bool) -> None:
    starter = agent(db_url, code, "a")
    releaser = agent(db_url, code, "a")
    old = pieces.start(starter, "media", ["src/media/store.py"], repo).task
    run_out(db_url, old.id)
    with_lock = threading.Event()
    finish = threading.Event()
    first = starter if start_first else releaser
    mutation = "_create_intent" if start_first else "_complete_task"
    original = getattr(first, mutation)
    blocking_pid = None

    def pause(conn: psycopg.Connection[Any], *args: Any) -> Any:
        nonlocal blocking_pid
        if blocking_pid is None:
            blocking_pid = conn.info.backend_pid
            with_lock.set()
            assert finish.wait(10), "timed out waiting to finish the first operation"
        return original(conn, *args)

    monkeypatch.setattr(first, mutation, pause)
    restart = partial(pieces.start, starter, "media", [], repo)
    release = partial(pieces.done, releaser, "media")
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(restart if start_first else release)
        try:
            assert with_lock.wait(10), "first operation did not reach its mutation"
            b = pool.submit(release if start_first else restart)
            with psycopg.connect(db_url, autocommit=True) as observer:
                deadline = time.monotonic() + 8
                while observer.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE NOT granted AND locktype = 'advisory'"
                    " AND %s = ANY(pg_blocking_pids(pid)))", (blocking_pid,),
                ).fetchone() != (True,):
                    assert not b.done() and time.monotonic() < deadline, "second operation did not wait for the lock"
                    time.sleep(0.01)
        finally:
            finish.set()
        first_result = a.result(timeout=10)
        if start_first:
            assert first_result.task.id != old.id
            assert b.result(timeout=10).id == first_result.task.id
        else:
            assert first_result.id == old.id
            with pytest.raises(BenchbookError, match="--paths"):
                b.result(timeout=10)
    assert pieces.held(starter) == []


def test_released_generation_wins_even_if_clock_steps_back(db_url: str, code: str, repo: Path) -> None:
    a = agent(db_url, code, "a")
    old = pieces.start(a, "media", ["src/media/store.py"], repo).task
    run_out(db_url, old.id)
    assert a.next_task() is None  # auto-failure keeps the old deadline
    new = pieces.start(a, "media", [], repo).task
    # Simulate chronology and timestamps disagreeing after system clock correction.
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE benchbook.tasks SET created_at = clock_timestamp() + interval '1 day' WHERE id=%s",
                     (old.id,))
    assert pieces.done(a, "media").id == new.id
    with pytest.raises(BenchbookError, match="--paths"):
        pieces.start(a, "media", [], repo)


def test_done_releases_and_a_lead_can_release_anyones(db_url: str, code: str, repo: Path) -> None:
    a, b = agent(db_url, code, "a"), agent(db_url, code, "b")
    lead = agent(db_url, code, "lead", "orchestrator")
    s = pieces.start(a, "media", ["src/media/*"], repo)
    with pytest.raises(BenchbookError, match=f"held by a \\({s.task.short_id}\\)"):
        pieces.done(b, "media")
    assert pieces.done(a, "media", summary="merged").status == "done"
    t = pieces.start(b, "store", ["src/media/store.py"], repo).task
    assert pieces.done(lead, "store").status == "cancelled"  # the lead released b's piece
    assert lead.get_task(t.id).status == "cancelled"
    with pytest.raises(BenchbookError, match="no live piece"):
        pieces.done(a, "media")
    pieces.start(a, "media", ["src/media/*"], repo)
    assert pieces.done(a, "media", failed=True).status == "failed"


def test_pieces_are_read_through_v_inflight(db_url: str, code: str, repo: Path) -> None:
    """The code pack's brief shows occupied paths per module tag: the reader view already carries them."""
    a = agent(db_url, code, "a")
    pieces.start(a, "media", ["src/media/*"], repo, tags=["mod:media"])
    pieces.start(a, "auth", ["src/auth/login.py"], repo, tags=["mod:auth"])
    lead = agent(db_url, code, "lead", "orchestrator")
    rows = lead.sql("SELECT agent, spec ->> 'piece' AS piece, spec -> 'paths' AS paths FROM v_inflight"
                    " WHERE is_intent AND tags @> ARRAY['mod:media']")["rows"]
    assert rows == [{"agent": "a", "piece": "media", "paths": ["src/media/store.py", "src/media/upload.py"]}]
    held = pieces.held(lead, ["mod:auth"])
    assert [(h["piece"], h["agent"], h["tags"], h["declared"]) for h in held] == [
        ("auth", "a", ["mod:auth"], ["src/auth/login.py"])]
    # the query docs/code.md gives for a brief (not a sql block: tests/test_checks.py reads the only one)
    [query] = re.findall(r"```\n(SELECT spec ->> 'piece'.*?)```", DOC.read_text(), re.S)
    assert [(r["piece"], r["agent"], r["paths"]) for r in lead.sql(query)["rows"]] == [
        ("media", "a", ["src/media/store.py", "src/media/upload.py"])]
    again = pieces.start(a, "auth", ["src/auth/**"], repo, tags=["mod:other"])
    assert again.note is not None and "mod:other" in again.note  # tags are fixed at the first start


def test_bad_input(db_url: str, code: str, repo: Path, tmp_path: Path) -> None:
    a = agent(db_url, code, "a")
    with pytest.raises(ValidationFailed, match="not a usable glob"):
        pieces.start(a, "x", ["src/[z-a]"], repo)
    with pytest.raises(ValidationFailed, match="piece"):
        pieces.start(a, "two words", ["src/**"], repo)
    with pytest.raises(BenchbookError, match="git"):
        pieces.start(a, "x", ["src/**"], tmp_path / "nowhere")
    with pytest.raises(ValidationFailed, match="spec.paths"):  # the ledger checks a path field it is asked to
        a.create_intent("raw", {"paths": "src/**"}, exclusive_paths=["paths"])
    core = make_project(db_url)  # a project without worker_intents: workers cannot start pieces there
    with pytest.raises(PermissionDenied, match="worker_intents"):
        pieces.start(agent(db_url, core, "w"), "x", ["src/**"], repo)


def test_knob_intents_ignore_paths(db_url: str, code: str, repo: Path) -> None:
    """exclusive_paths is opt-in: an intent that does not ask for it is not checked against piece paths."""
    a, b = agent(db_url, code, "a"), agent(db_url, code, "b")
    pieces.start(a, "media", ["src/media/*"], repo)
    b.create_intent("raw intent naming the same paths", {"paths": ["src/media/upload.py"]})


def test_under_tokens_the_piece_is_the_tokens_agent(db_url: str, repo: Path) -> None:
    d = load_pack("code").project_defaults
    pid = make_project(db_url, metric=d["metric"], direction=d["direction"], pack="code", settings=dict(d["settings"]))
    lead = token_ledger(db_url, pid, "lead")
    a, b = token_ledger(db_url, pid, "impl-a", "worker"), token_ledger(db_url, pid, "impl-b", "worker")
    s = pieces.start(a, "media", ["src/media/*"], repo, tags=["mod:media"])
    assert (s.task.created_by, s.task.lease_owner) == ("impl-a", "impl-a")
    with pytest.raises(Conflict, match=s.task.short_id):
        pieces.start(b, "store", ["src/media/store.py"], repo)
    assert pieces.start(a, "media", [], repo).renewed  # renew over the writer login
    assert [(h["piece"], h["agent"]) for h in pieces.held(lead)] == [("media", "impl-a")]
    with pytest.raises(BenchbookError, match="held by impl-a"):
        pieces.done(b, "media")
    assert pieces.done(a, "media").status == "done"
    pieces.start(b, "store", ["src/media/store.py"], repo)
    assert pieces.done(lead, "store").status == "cancelled"
    events = [(ev["event"], ev["agent_id"]) for ev in lead.get(s.task.id)["events"]]
    assert events[0] == ("created", "impl-a") and events[-1] == ("done", "impl-a")


# ------------------------------------------------------------------------------------------- CLI

def cli(repo: Path, db_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url}
    for k in ("BENCHBOOK_PROJECT", "BENCHBOOK_AGENT", "BENCHBOOK_TOKEN", "ASSAY_PROJECT", "ASSAY_AGENT"):
        env.pop(k, None)
    return subprocess.run([sys.executable, "-m", "benchbook.cli", *args], cwd=repo, env=env, capture_output=True,
                          text=True, timeout=120)


def test_cli_output_and_exit_codes(db_url: str, repo: Path) -> None:
    pid = f"code-{uuid.uuid4().hex[:6]}"
    assert cli(repo, db_url, "init", "--project", pid, "--pack", "code").returncode == 0
    p = cli(repo, db_url, "--project", pid, "--agent", "a", "piece", "start", "media", "--paths", "src/media/*",
            "--paths", "src/media/new_*.py", "--tags", "mod:media")
    assert p.returncode == 0, p.stderr
    tid = p.stdout.split("(")[1].split(")")[0]
    assert p.stdout.startswith(f"started piece media ({tid}) for a: 3 path(s), lease 2h00m")
    assert "    src/media/upload.py" in p.stdout
    assert "no tracked file yet (kept as globs): src/media/new_*.py" in p.stdout
    p = cli(repo, db_url, "--project", pid, "--agent", "b", "piece", "start", "uploads", "--paths", "src/**/up*.py")
    assert p.returncode == 2 and p.stdout == ""
    assert p.stderr.startswith(f"error: {tid} by a already holds these paths (paths): ") and "piece media" in p.stderr
    p = cli(repo, db_url, "--project", pid, "--agent", "b", "--json", "piece", "start", "uploads", "--paths", "src/**")
    assert p.returncode == 2 and json.loads(p.stdout)["ok"] is False and tid in json.loads(p.stdout)["error"]
    p = cli(repo, db_url, "--project", pid, "piece", "list")  # a read: no name needed
    assert p.returncode == 0 and p.stdout.startswith(f"{tid}  media  a  ") and "[mod:media]" in p.stdout
    p = cli(repo, db_url, "--project", pid, "--json", "piece", "list", "--tags", "mod:media")
    [row] = json.loads(p.stdout)
    assert row["piece"] == "media" and row["paths"] == ["src/media/new_*.py", "src/media/store.py",
                                                        "src/media/upload.py"]
    p = cli(repo, db_url, "--project", pid, "--agent", "a", "--json", "piece", "start", "media")
    assert p.returncode == 0 and json.loads(p.stdout)["renewed"] is True and json.loads(p.stdout)["task"] == tid
    p = cli(repo, db_url, "--project", pid, "--agent", "a", "piece", "done", "media", "-m", "merged")
    assert p.returncode == 0 and p.stdout.strip() == f"released piece media ({tid}, done)"
    assert cli(repo, db_url, "--project", pid, "piece", "list").stdout.strip() == "(no live pieces)"
    p = cli(repo, db_url, "--project", pid, "--agent", "a", "piece", "done", "media")
    assert p.returncode == 1 and "no live piece media" in p.stderr
    p = cli(repo, db_url, "--project", pid, "--agent", "b", "piece", "start", "uploads")
    assert p.returncode == 1 and "--paths" in p.stderr
    p = cli(repo, db_url, "--project", pid, "--agent", "b", "piece", "start", "uploads", "--paths", "src/[z-a]")
    assert p.returncode == 1 and "not a usable glob" in p.stderr
    p = cli(repo, db_url, "--project", pid, "--agent", "b", "piece", "start", "uploads", "--paths", "src/**/up*.py")
    assert p.returncode == 0, p.stderr
