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


@pytest.fixture(scope="session")
def db_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway database: BENCHBOOK_TEST_DATABASE_URL (CI service) or a fresh embedded Postgres."""
    if base := os.environ.get("BENCHBOOK_TEST_DATABASE_URL"):
        name = f"benchbook_test_{uuid.uuid4().hex[:8]}"
        url = dbconn.ensure_database(base, name)
        yield url
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        return
    from pixeltable_pgserver import get_server

    pgdata = Path(tmp_path_factory.mktemp("pg")) / "data"
    server = get_server(pgdata, cleanup_mode=None, postgres_version=dbconn.EMBEDDED_PG_VERSION)
    url = dbconn.ensure_database(server.get_uri(), "benchbook_test")
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
