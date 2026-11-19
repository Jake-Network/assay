"""Schema migrations via Alembic. Each revision executes a raw SQL file from alembic/sql/,
so the schema stays reviewable as plain SQL (spec §16.1: no ORM).

Alembic/SQLAlchemy are imported only when an upgrade is actually needed: the version check is plain
SQL, which keeps CLI and hook start-up fast (the hook must finish within 1s, §12.3)."""
from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import psycopg

ALEMBIC_DIR = Path(str(resources.files("assay.db").joinpath("alembic")))


def head() -> str:
    """Revision ids are the NNNN prefix of the version files, in a straight line."""
    revs = sorted(p.name.split("_", 1)[0] for p in (ALEMBIC_DIR / "versions").glob("[0-9]*_*.py"))
    return revs[-1]


def run_sql_file(name: str) -> None:
    from alembic import op

    sql = (ALEMBIC_DIR / "sql" / name).read_text(encoding="utf-8")
    # Run on the raw psycopg connection with no params, so % and : in the SQL are left alone.
    raw: Any = op.get_bind().connection.driver_connection
    raw.execute(sql.encode("utf-8"))  # bytes: psycopg sends it as one simple-query script
    raw.execute("RESET search_path")


def sa_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def alembic_config(url: str) -> Any:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", sa_url(url).replace("%", "%%"))
    return cfg


def current(url: str) -> str | None:
    with psycopg.connect(url) as conn:
        reg = conn.execute("SELECT to_regclass('assay.alembic_version')::text").fetchone()
        if reg is None or reg[0] is None:
            return None
        row = conn.execute("SELECT version_num FROM assay.alembic_version").fetchone()
        return str(row[0]) if row else None


def migrate(url: str) -> tuple[str | None, str]:
    """Upgrade to head. Returns (before, after). Safe to call concurrently (advisory lock in env.py)."""
    before = current(url)
    if before != head():
        from alembic import command

        command.upgrade(alembic_config(url), "head")
    return before, head()
