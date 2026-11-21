from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from benchbook import Ledger
from benchbook.db import conn as dbconn
from benchbook.db import migrate as dbmigrate

from .legacy_db import add_marker_project, build_legacy

# Set to 1 to build the session database the way the pre-rename releases did (old schema and role names, one row of
# data) and let migrate() rename it, so every test, the invariants and their triggers included, runs on an upgraded
# database. tests/test_db.py runs the invariant suite this way.
LEGACY_SWITCH = "BENCHBOOK_TEST_LEGACY_DB"


def _through_the_rename(url: str) -> None:
    if os.environ.get(LEGACY_SWITCH):
        build_legacy(url)
        add_marker_project(url)
        before, _ = dbmigrate.migrate(url)
        assert before == dbmigrate.LEGACY_HEAD


@pytest.fixture(scope="session", autouse=True)
def _throwaway_dirs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep every test away from the developer's own benchbook: the default state and data directories resolve under
    a temporary folder, and pre-rename ASSAY_* variables of the shell are not read as a fallback. Without this a
    test that forgets XDG_STATE_HOME would write into ~/.local/state, where an install from before the rename keeps
    its hook spool (benchbook.config.state_dir prefers that folder while it is the only one)."""
    base = tmp_path_factory.mktemp("xdg")
    saved = {k: v for k, v in os.environ.items() if k.startswith("ASSAY_") or k in ("XDG_STATE_HOME", "XDG_DATA_HOME")}
    for k in saved:
        del os.environ[k]
    os.environ["XDG_STATE_HOME"] = str(base / "state")
    os.environ["XDG_DATA_HOME"] = str(base / "data")
    yield
    for k in ("XDG_STATE_HOME", "XDG_DATA_HOME"):
        os.environ.pop(k, None)
    os.environ.update(saved)


@pytest.fixture(scope="session")
def db_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway database: BENCHBOOK_TEST_DATABASE_URL (CI service) or a fresh embedded Postgres."""
    if base := os.environ.get("BENCHBOOK_TEST_DATABASE_URL"):
        name = f"benchbook_test_{uuid.uuid4().hex[:8]}"
        url = dbconn.ensure_database(base, name)
        _through_the_rename(url)
        yield url
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        return
    from pixeltable_pgserver import get_server

    pgdata = Path(tmp_path_factory.mktemp("pg")) / "data"
    server = get_server(pgdata, cleanup_mode=None, postgres_version=dbconn.EMBEDDED_PG_VERSION)
    url = dbconn.ensure_database(server.get_uri(), "benchbook_test")
    _through_the_rename(url)
    os.environ["BENCHBOOK_DATABASE_URL"] = url
    yield url
    for pool in Ledger._pools.values():
        pool.close()
    subprocess.run([str(server.bin_path / "pg_ctl"), "-D", str(pgdata), "-m", "immediate", "stop"],
                   capture_output=True, check=False)
    shutil.rmtree(pgdata, ignore_errors=True)


def make_project(db_url: str, metric: str | None = "score", direction: str | None = "max",
                 **kw: object) -> str:
    pid = f"t-{uuid.uuid4().hex[:10]}"
    Ledger.init_project(pid, goal="Maximise score.", metric=metric, direction=direction,
                        database_url=db_url, **kw)  # type: ignore[arg-type]
    return pid


@pytest.fixture
def project(db_url: str) -> str:
    return make_project(db_url)


@pytest.fixture
def lead(db_url: str, project: str) -> Ledger:
    return Ledger.open(project=project, agent="lead", database_url=db_url)


@pytest.fixture
def worker(db_url: str, project: str) -> Ledger:
    return Ledger.open(project=project, agent="worker-1", role="worker", database_url=db_url)
