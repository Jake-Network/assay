"""Hook retries preserve one ledger experiment per queued check."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO, Any

import psycopg
import pytest

from benchbook import hooks
from benchbook.core import identity
from benchbook.core.ledger import Ledger
from tests.test_review_holes import code_project, owner_rows, tok
from tests.test_second_review import _capture_entry
from tests.writer import as_writer


def _spool(monkeypatch: pytest.MonkeyPatch, token: str) -> Path:
    monkeypatch.setenv("BENCHBOOK_TOKEN", token)
    spool = hooks._spool_dir()
    spool.mkdir(parents=True, exist_ok=True)
    return spool


def _queue(spool: Path, project: str, repo: Path) -> Path:
    paths = set(spool.glob("*.json"))
    hooks._write_spool(spool, {"event": "check-run", "project": project, "accepted": True, "data": {
        "check": "unit", "piece": "piece", "status": "ok", "sha": "a" * 40, "exit_code": 0, "repo": str(repo),
    }})
    [path] = set(spool.glob("*.json")) - paths
    return path


def _start(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    project = code_project(db_url)
    _, old = tok(db_url, project, "worker", "worker")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    monkeypatch.delenv("BENCHBOOK_AGENT", raising=False)
    spool = _spool(monkeypatch, old)
    hooks._write_spool(spool, _capture_entry(project, "recorded under the previous token"))
    assert hooks.replay_now(spool).captured == 1
    return project, spool


def _rotate(db_url: str, project: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _spool(monkeypatch, identity.add_agent(project, "worker", rotate=True, database_url=db_url).token)


def _experiments(db_url: str, project: str) -> list[dict[str, Any]]:
    return owner_rows(db_url, "SELECT id, author, duplicate_of FROM benchbook.experiments WHERE project_id = %s",
                      (project,))


@pytest.mark.parametrize("failure", ["unlink", "marker"])
def test_retry_after_adopted_experiment_commits(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                failure: str) -> None:
    project, old = _start(db_url, tmp_path, monkeypatch)
    _queue(old, project, tmp_path)
    spool = _rotate(db_url, project, monkeypatch)
    unlink, open_path = Path.unlink, Path.open

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.parent == spool and path.name.startswith(hooks.ADOPTED_PREFIX):
            raise PermissionError("destination file could not be removed")
        unlink(path, missing_ok=missing_ok)

    def fail_marker(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> IO[Any]:
        if path == spool / hooks.ADOPTED_DONE and mode == "a":
            raise OSError("completion marker could not be saved")
        return open_path(path, mode, *args, **kwargs)

    with monkeypatch.context() as fault:
        if failure == "unlink":
            fault.setattr(Path, "unlink", fail_unlink)
        else:
            fault.setattr(Path, "open", fail_marker)
        result = hooks.replay_now(spool)
    assert result.adopted == result.queued == 1
    before = _experiments(db_url, project)
    assert len(before) == 1 and before[0]["author"] == "worker"
    assert hooks.replay_now(spool).ok
    assert _experiments(db_url, project) == before
    assert not list(spool.glob("*.json"))


def test_same_receipt_is_atomic_and_distinct_receipts_can_repeat(db_url: str, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    project, spool = _start(db_url, tmp_path, monkeypatch)
    path = _queue(spool, project, tmp_path)
    copy = spool / "same-receipt.json"
    copy.write_bytes(path.read_bytes())
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(hooks.process_file, (path, copy)))
    assert outcomes[0].startswith("ok:recorded:e:") and outcomes[0] == outcomes[1]
    [original] = _experiments(db_url, project)
    _queue(spool, project, tmp_path)
    assert hooks.replay_now(spool).recorded == 1
    [repeat] = [row for row in _experiments(db_url, project) if row["id"] != original["id"]]
    assert repeat["duplicate_of"] == original["id"]


def test_receipt_survives_repeated_rotation(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, old = _start(db_url, tmp_path, monkeypatch)
    original = _queue(old, project, tmp_path)
    middle = _rotate(db_url, project, monkeypatch)
    unlink, open_path = Path.unlink, Path.open

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == original:
            raise PermissionError("source file could not be removed")
        unlink(path, missing_ok=missing_ok)

    def fail_marker(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> IO[Any]:
        if path == middle / hooks.ADOPTED_DONE and mode == "a":
            raise OSError("completion marker could not be saved")
        return open_path(path, mode, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", fail_unlink)
        fault.setattr(Path, "open", fail_marker)
        first = hooks.replay_now(middle)
    assert first.adopted == 1 and first.queued == 2
    current = _rotate(db_url, project, monkeypatch)
    final = hooks.replay_now(current)
    assert final.ok and final.adopted == final.recorded == 1
    assert len(_experiments(db_url, project)) == 1
    assert not list(old.glob("*.json")) and not list(middle.glob("*.json"))


def test_older_unconfirmed_spool_is_reported(db_url: str, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    project = code_project(db_url)
    _, old = tok(db_url, project, "worker", "worker")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    spool = _spool(monkeypatch, old)
    queued = _queue(spool, project, tmp_path)
    (spool / hooks.WRITER_INFO).unlink()  # folder created by a version that wrote metadata only after auth
    result = hooks.replay_now(_rotate(db_url, project, monkeypatch))
    assert result.held == 1 and not result.ok and queued.exists()


def test_database_outage_is_reported_when_adopting(db_url: str, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    project, old = _start(db_url, tmp_path, monkeypatch)
    paths = [_queue(old, project, tmp_path) for _ in range(2)]
    spool = _rotate(db_url, project, monkeypatch)
    looked_up: list[str] = []

    def unavailable(project: str) -> Ledger:
        looked_up.append(project)
        raise psycopg.OperationalError("database unavailable")

    with monkeypatch.context() as fault:
        fault.setattr(Ledger, "open", unavailable)
        result = hooks.replay_now(spool)
    assert looked_up == [project]
    assert result.queued == 2 and not result.ok and all(path.exists() for path in paths)
    [error] = result.errors
    assert "could not confirm the current token's agent" in error
    assert hooks.replay_now(spool).recorded == 2


def test_confirmed_queue_in_another_project_is_ignored(db_url: str, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    project, old = _start(db_url, tmp_path, monkeypatch)
    pending = _queue(old, project, tmp_path)
    another = code_project(db_url)
    _, other_token = tok(db_url, another, "worker", "worker")
    result = hooks.replay_now(_spool(monkeypatch, other_token))
    assert result.ok and result.processed == 0 and result.errors == [] and pending.exists()
