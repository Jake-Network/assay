"""Database location: an explicit URL (team mode) or an embedded Postgres (local mode, P7).

All knowledge of the embedded server package lives in this module so it can be swapped.
"""
from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

import psycopg

# the embedded server logs at INFO; keep stderr quiet for CLI, hooks and stdio MCP
logging.getLogger("pixeltable_pgserver").setLevel(logging.WARNING)

EMBEDDED_PG_VERSION = 16
EMBEDDED_DB = "assay"


def data_dir() -> Path:
    if env := os.environ.get("ASSAY_DATA_DIR"):
        return Path(env).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "assay" / "pg"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "assay" / "pg"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "assay"


def _embedded_server_uri(pgdata: Path) -> str:
    # Imported lazily: team mode never needs the embedded binaries.
    from pixeltable_pgserver.postgres_server import get_server

    pgdata.parent.mkdir(parents=True, exist_ok=True)
    # cleanup_mode=None: the server outlives the process that started it, so one Claude Code
    # session ending never cuts off the others (DECISIONS.md, M0). The package serialises
    # concurrent start-up with an inter-process file lock. `assay db stop` stops it.
    server = get_server(pgdata, cleanup_mode=None, postgres_version=EMBEDDED_PG_VERSION)
    return server.get_uri()


def _with_db(uri: str, dbname: str) -> str:
    # pixeltable_pgserver URIs look like postgresql://postgres:@/postgres?host=/tmp/xyz
    head, sep, query = uri.partition("?")
    base = head.rsplit("/", 1)[0]
    return f"{base}/{dbname}{sep}{query}"


def ensure_database(server_uri: str, dbname: str) -> str:
    with psycopg.connect(server_uri, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            try:
                conn.execute(f'CREATE DATABASE "{dbname}"')
            except psycopg.errors.DuplicateDatabase:
                pass
    return _with_db(server_uri, dbname)


def database_url(explicit: str | None = None) -> str:
    """Resolve the database URL: argument, then ASSAY_DATABASE_URL, then embedded Postgres."""
    url = explicit or os.environ.get("ASSAY_DATABASE_URL")
    if url:
        return url
    return ensure_database(_embedded_server_uri(data_dir()), EMBEDDED_DB)


def stop_embedded() -> bool:
    from pixeltable_pgserver.postgres_server import get_server

    d = data_dir()
    if not (d / "PG_VERSION").exists():
        return False
    server = get_server(d, cleanup_mode=None, postgres_version=EMBEDDED_PG_VERSION, start=False)
    import subprocess

    subprocess.run([str(server.bin_path / "pg_ctl"), "-D", str(d), "-m", "fast", "stop"],
                   capture_output=True, check=False)
    return True
