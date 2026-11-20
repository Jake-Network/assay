"""Embedded server start-up and schema version checks (U1, I6)."""
from __future__ import annotations

import platform
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest

from assay.db import conn as dbconn
from assay.db import migrate as dbmigrate
from assay.errors import AssayError


@pytest.mark.skipif(platform.system() == "Windows", reason="unix sockets")
def test_embedded_server_in_a_data_dir_with_a_space(tmp_path: Path) -> None:
    # macOS default: ~/Library/Application Support/assay/pg
    pgdata = tmp_path / "Application Support" / "assay" / "pg"
    uri = dbconn._embedded_server_uri(pgdata)
    try:
        with psycopg.connect(uri) as c:
            assert c.execute("SELECT 1").fetchone() == (1,)
        assert " " not in uri
    finally:
        from pixeltable_pgserver.postgres_server import PostgresServer

        server = PostgresServer._instances.get(pgdata)
        bin_path = server.bin_path if server else None
        if bin_path is not None:
            subprocess.run([str(bin_path / "pg_ctl"), "-D", str(pgdata), "-m", "immediate", "stop"],
                           capture_output=True, check=False)


def test_start_failure_is_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pixeltable_pgserver import postgres_server

    pgdata = tmp_path / "pg"
    pgdata.mkdir()
    (pgdata / "log").write_text("LOG:  starting\nFATAL:  invalid argument: \"Support/assay/pg\"\n")

    def broken(*a: object, **k: object) -> None:
        raise subprocess.CalledProcessError(1, ["pg_ctl", "start"])

    monkeypatch.setattr(postgres_server, "get_server", broken)
    with pytest.raises(AssayError) as ei:
        dbconn._embedded_server_uri(pgdata)
    msg = str(ei.value)
    assert "invalid argument" in msg and "ASSAY_DATA_DIR" in msg and str(pgdata) in msg


def test_newer_schema_fails_with_upgrade_hint(db_url: str) -> None:
    name = f"assay_newer_{uuid.uuid4().hex[:8]}"
    url = dbconn.ensure_database(db_url, name)
    try:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("CREATE SCHEMA assay")
            c.execute("CREATE TABLE assay.alembic_version (version_num varchar(32) PRIMARY KEY)")
            c.execute("INSERT INTO assay.alembic_version VALUES ('9999')")
        with pytest.raises(AssayError, match=r"revision 9999 is newer than this assay \(head \d{4}\); upgrade assay"):
            dbmigrate.migrate(url)
    finally:
        with psycopg.connect(db_url, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
