"""Database location: an explicit URL (team mode) or an embedded Postgres (local mode, P7).

All knowledge of the embedded server package lives in this module so it can be swapped.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
from pathlib import Path

import psycopg

from .. import config
from ..config import env

# the embedded server logs at INFO; keep stderr quiet for CLI, hooks and stdio MCP
logging.getLogger("pixeltable_pgserver").setLevel(logging.WARNING)

EMBEDDED_PG_VERSION = 16
EMBEDDED_DB = "benchbook"
LEGACY_EMBEDDED_DB = "assay"  # the database's name in an embedded cluster created before the rename


def notification_channel(project_id: str) -> str:
    """Match the channel selected by the notify_insert trigger."""
    slug = project_id.encode("utf-8")
    if len(slug) <= 53:  # "benchbook_" + slug must fit Postgres's 63-byte channel name
        return f"benchbook_{project_id}"
    digest = hashlib.md5(slug, usedforsecurity=False).hexdigest()
    return f"benchbook_:{digest}"


def default_data_dirs() -> tuple[Path, Path]:
    """(the default data directory of the embedded Postgres, the one from before the rename)."""
    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / config.NAME / "pg", base / config.LEGACY_NAME / "pg"


def data_dir() -> Path:
    """BENCHBOOK_DATA_DIR, else the default. A cluster created before the rename stays in use where it is while the
    new location holds none: a server may be running in it, so benchbook never moves the folder (doctor says how)."""
    if explicit := env("DATA_DIR"):
        return Path(explicit).expanduser()
    new, old = default_data_dirs()
    return old if config.prefer_legacy(new, old, marker="PG_VERSION") else new


def state_dir() -> Path:
    return config.state_dir()


def _shell_safe(p: Path) -> bool:
    return not any(c.isspace() or c in "'\"\\" for c in str(p))


def _install_socket_dir_guard() -> None:
    """The embedded server passes its socket directory unquoted inside pg_ctl's `-o` string, so a data dir with a
    space (macOS: ~/Library/Application Support) splits the argument and postgres refuses to start. Keep the socket
    in a directory without spaces; the data itself stays where it is."""
    from pixeltable_pgserver import postgres_server, utils

    original = utils.find_suitable_socket_dir
    if getattr(getattr(postgres_server, "find_suitable_socket_dir", None), "_benchbook_guard", False):
        return

    def guarded(pgdata: Path, runtime_path: Path) -> Path:
        if _shell_safe(pgdata) and _shell_safe(runtime_path):
            return original(pgdata, runtime_path)
        import tempfile

        key = hashlib.sha256(str(pgdata.resolve()).encode()).hexdigest()[:10]
        for base in (runtime_path, Path(tempfile.gettempdir()), Path("/tmp")):
            d = base / f"benchbook-{key}"
            if not _shell_safe(d):
                continue
            try:
                d.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                continue
            if utils.socket_name_length_ok(d / ".s.PGSQL.5432"):
                return d
        return original(pgdata, runtime_path)

    guarded._benchbook_guard = True  # type: ignore[attr-defined]
    setattr(postgres_server, "find_suitable_socket_dir", guarded)  # noqa: B010 (not exported, so not typed)


def _log_tail(pgdata: Path, lines: int = 5) -> str:
    try:
        text = (pgdata / "log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no server log)"
    tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join(tail) or "(empty server log)"


def _embedded_server_uri(pgdata: Path) -> str:
    # Imported lazily: team mode never needs the embedded binaries.
    import subprocess

    from pixeltable_pgserver.postgres_server import get_server

    from ..errors import BenchbookError

    pgdata.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        _install_socket_dir_guard()
    # cleanup_mode=None: the server outlives the process that started it, so one Claude Code
    # session ending never cuts off the others (DECISIONS.md, M0). The package serialises
    # concurrent start-up with an inter-process file lock. `benchbook db stop` stops it.
    try:
        server = get_server(pgdata, cleanup_mode=None, postgres_version=EMBEDDED_PG_VERSION)
    except (subprocess.SubprocessError, RuntimeError, OSError) as e:
        raise BenchbookError(
            f"the embedded Postgres in {pgdata} failed to start ({type(e).__name__}).\n"
            f"Last lines of {pgdata / 'log'}:\n{_log_tail(pgdata)}\n"
            "Set BENCHBOOK_DATA_DIR to another directory (for example ~/.local/share/benchbook/pg), "
            "or BENCHBOOK_DATABASE_URL "
            "to use an existing Postgres. CLI, MCP server and hooks must all see the same value."
        ) from e
    return server.get_uri()


def _with_db(uri: str, dbname: str) -> str:
    # pixeltable_pgserver URIs look like postgresql://postgres:@/postgres?host=/tmp/xyz
    head, sep, query = uri.partition("?")
    base = head.rsplit("/", 1)[0]
    return f"{base}/{dbname}{sep}{query}"


def ensure_database(server_uri: str, dbname: str, legacy: str | None = None) -> str:
    """The URL of database dbname on the server, created when missing. legacy: the name it had before the rename;
    when that database exists and dbname does not, it is the ledger, and it is used under its old name."""
    with psycopg.connect(server_uri, autocommit=True) as conn:
        found = {r[0] for r in conn.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)",
                                            ([n for n in (dbname, legacy) if n],))}
        if dbname not in found:
            if legacy in found:
                return _with_db(server_uri, str(legacy))
            try:
                conn.execute(f'CREATE DATABASE "{dbname}"')
            except psycopg.errors.DuplicateDatabase:
                pass
    return _with_db(server_uri, dbname)


def database_url(explicit: str | None = None) -> str:
    """Resolve the database URL: argument, then BENCHBOOK_DATABASE_URL, then embedded Postgres."""
    url = explicit or env("DATABASE_URL")
    if url:
        return url
    return ensure_database(_embedded_server_uri(data_dir()), EMBEDDED_DB, legacy=LEGACY_EMBEDDED_DB)


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
