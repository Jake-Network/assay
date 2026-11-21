"""Databases as the pre-rename releases left them: schema assay, reader schema assay_q, role assay_reader.

The 0001-0007 SQL files only ever ran under the old names. Reversing the mechanical rename gives back the text those
releases executed (compared byte for byte with the last pre-rename commit when this was written), so the string
literals inside function and view bodies are the old ones too. Nothing here reads git history: the tests also run
from an sdist."""
from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg import sql

from benchbook.db import migrate as dbmigrate

LEGACY_FILES = sorted(p.name for p in (dbmigrate.ALEMBIC_DIR / "sql").glob("[0-9]*_*.sql")
                      if p.name[:4] <= dbmigrate.LEGACY_HEAD)
# insertion order (foreign keys); the last four have a bigserial id that the copy lets the sequence assign
TABLES = ["projects", "agents", "tasks", "sources", "experiments", "claims", "links", "decisions", "blobs",
          "task_events", "dedup_hits", "brief_log", "settings_events"]
SERIAL = {"task_events", "dedup_hits", "brief_log", "settings_events"}
MARKER_PROJECT = "legacy-origin"


def legacy_sql(name: str) -> str:
    text = (dbmigrate.ALEMBIC_DIR / "sql" / name).read_text(encoding="utf-8")
    text = text.replace("benchbook", "assay").replace("BENCHBOOK", "ASSAY")
    if name.startswith("0004"):
        new = "octet_length(NEW.project_id) <= 53"  # "assay_" is four bytes shorter than "benchbook_"
        assert text.count(new) == 1
        text = text.replace(new, "octet_length(NEW.project_id) <= 57")
    return text


def build_legacy(url: str, revision: str = dbmigrate.LEGACY_HEAD) -> None:
    """Apply the old-named SQL up to `revision` and stamp assay.alembic_version the way Alembic did."""
    with psycopg.connect(url, autocommit=True) as conn:
        for name in LEGACY_FILES:
            if name[:4] <= revision:
                conn.execute(legacy_sql(name).encode("utf-8"))
                conn.execute("RESET search_path")
        conn.execute("CREATE TABLE assay.alembic_version (version_num VARCHAR(32) NOT NULL,"
                     " CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
        conn.execute("INSERT INTO assay.alembic_version VALUES (%s)", (revision,))


def add_marker_project(url: str) -> None:
    """A row written while the schema still had its old name, to recognise an upgraded database by."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("INSERT INTO assay.projects(id, title, goal) VALUES (%s, 'before the rename', 'none')",
                     (MARKER_PROJECT,))
        conn.execute("INSERT INTO assay.agents(project_id, id, role) VALUES (%s, 'lead', 'orchestrator')",
                     (MARKER_PROJECT,))


def table_rows(url: str, schema: str, project: str) -> dict[str, list[Any]]:
    """Every row of the project as JSON, per table, in a stable order."""
    out: dict[str, list[Any]] = {}
    with psycopg.connect(url) as conn:
        for table in TABLES:
            key = "id" if table == "projects" else "project_id"
            rows = [r[0] for r in conn.execute(
                sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE {} = %s").format(
                    sql.Identifier(schema, table), sql.Identifier(key)), (project,))]
            out[table] = sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))
    return out


def copy_project_as_legacy(src_url: str, project: str, dst_url: str) -> None:
    """Insert a project's rows (read from a current database, where the SDK wrote them) through the old-named SQL, so
    the old triggers run. Blob URIs get the prefix the old releases wrote."""
    with psycopg.connect(src_url) as src, psycopg.connect(dst_url, autocommit=True) as dst:
        for table in TABLES:
            key = "id" if table == "projects" else "project_id"
            order = "t.id" if table in SERIAL else "t.created_at"
            rows = src.execute(
                sql.SQL("SELECT to_jsonb(t)::text FROM {} t WHERE {} = %s ORDER BY {}").format(
                    sql.Identifier(dbmigrate.SCHEMA, table), sql.Identifier(key), sql.SQL(order)),
                (project,)).fetchall()
            target = sql.Identifier(dbmigrate.LEGACY_SCHEMA, table)
            names = [str(r[0]) for r in dst.execute(
                "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped"
                " ORDER BY attnum", (f"{dbmigrate.LEGACY_SCHEMA}.{table}",))]
            cols = sql.SQL(", ").join(sql.Identifier(c) for c in names if not (table in SERIAL and c == "id"))
            for (row,) in rows:
                dst.execute(
                    sql.SQL("INSERT INTO {} ({}) SELECT {} FROM jsonb_populate_record(NULL::{}, %s::jsonb)").format(
                        target, cols, cols, target),
                    (row.replace("benchbook://blob/", "assay://blob/"),))
