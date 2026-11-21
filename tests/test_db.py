"""Embedded server start-up, schema version checks (U1, I6) and the rename of a pre-rename database."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from benchbook import Ledger
from benchbook.db import conn as dbconn
from benchbook.db import migrate as dbmigrate
from benchbook.db.conn import notification_channel
from benchbook.errors import BenchbookError, PermissionDenied
from benchbook.ledger import BLOB_PREFIX, schema_version_ok

from .conftest import LEGACY_SWITCH, make_project
from .legacy_db import (
    LEGACY_FILES,
    MARKER_PROJECT,
    SERIAL,
    TABLES,
    build_legacy,
    copy_project_as_legacy,
    legacy_sql,
    table_rows,
)

ROOT = Path(__file__).resolve().parent.parent
LEGACY_REVISIONS = [name[:4] for name in LEGACY_FILES]


@pytest.mark.skipif(platform.system() == "Windows", reason="unix sockets")
def test_embedded_server_in_a_data_dir_with_a_space(tmp_path: Path) -> None:
    # macOS default: ~/Library/Application Support/benchbook/pg
    pgdata = tmp_path / "Application Support" / "benchbook" / "pg"
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
    (pgdata / "log").write_text("LOG:  starting\nFATAL:  invalid argument: \"Support/benchbook/pg\"\n")

    def broken(*a: object, **k: object) -> None:
        raise subprocess.CalledProcessError(1, ["pg_ctl", "start"])

    monkeypatch.setattr(postgres_server, "get_server", broken)
    with pytest.raises(BenchbookError) as ei:
        dbconn._embedded_server_uri(pgdata)
    msg = str(ei.value)
    assert "invalid argument" in msg and "BENCHBOOK_DATA_DIR" in msg and str(pgdata) in msg


def test_newer_schema_fails_with_upgrade_hint(db_url: str) -> None:
    name = f"benchbook_newer_{uuid.uuid4().hex[:8]}"
    url = dbconn.ensure_database(db_url, name)
    try:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute("CREATE SCHEMA benchbook")
            c.execute("CREATE TABLE benchbook.alembic_version (version_num varchar(32) PRIMARY KEY)")
            c.execute("INSERT INTO benchbook.alembic_version VALUES ('9999')")
        with pytest.raises(BenchbookError, match=r"revision 9999 is newer than this benchbook "
                                                 r"\(head \d{4}\); upgrade benchbook"):
            dbmigrate.migrate(url)
    finally:
        with psycopg.connect(db_url, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_version_check_is_one_query_when_nothing_needs_doing(db_url: str, project: str,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    # every CLI call and hook pays for this check (the fixture has migrated the database already)
    queries: list[Any] = []

    class Counting(psycopg.Connection[Any]):
        def execute(self, query: Any, *a: Any, **k: Any) -> Any:
            queries.append(query)
            return super().execute(query, *a, **k)

    monkeypatch.setattr(dbmigrate.psycopg, "connect", Counting.connect)
    assert dbmigrate.migrate(db_url) == (dbmigrate.head(), dbmigrate.head())
    assert len(queries) == 1
    assert dbmigrate.current(db_url) == dbmigrate.head() and len(queries) == 2
    assert schema_version_ok(db_url) and len(queries) == 3


# The rename of a pre-rename database (schema assay, assay_q, role assay_reader) ----------------------------------

class Cluster:
    """A private embedded server. Roles belong to a whole cluster and the session's server already has
    benchbook_reader, so a role rename can only be shown on a server of its own."""

    def __init__(self, pgdata: Path) -> None:
        from pixeltable_pgserver import get_server

        self.pgdata = pgdata
        self.server = get_server(pgdata, cleanup_mode=None, postgres_version=dbconn.EMBEDDED_PG_VERSION)
        self.base: str = self.server.get_uri()
        self.urls: list[str] = []

    def run(self, statement: str, url: str | None = None, params: Any = None) -> list[Any]:
        with psycopg.connect(url or self.base, autocommit=True) as c:
            cur = c.execute(statement.encode("utf-8"), params)
            return cur.fetchall() if cur.description else []

    def database(self, name: str) -> str:
        url = dbconn.ensure_database(self.base, name)
        self.urls.append(url)
        return url

    def legacy(self, name: str = "ledger", revision: str = dbmigrate.LEGACY_HEAD) -> str:
        url = self.database(name)
        build_legacy(url, revision)
        return url

    def roles(self) -> set[str]:
        return {r[0] for r in self.run("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' AND NOT rolsuper")}

    def renamed_roles(self) -> set[str]:
        """The roles the rename is about: without the ones a later migration creates on the way to head (0008's
        writer login), which exist or not depending on how far a test migrated."""
        return self.roles() - {"benchbook_writer"}

    def dump(self, url: str) -> str:
        """The schema as pg_dump prints it (owners left out), without the random \restrict key lines."""
        out = subprocess.run([str(self.server.bin_path / "pg_dump"), "--schema-only", "--no-owner", url],
                             capture_output=True, text=True, check=True).stdout
        return "\n".join(ln for ln in out.splitlines() if not ln.startswith(("\\restrict", "\\unrestrict")))

    def reset(self) -> None:
        for url in self.urls:
            pool = Ledger._pools.pop(url, None)
            if pool is not None:
                pool.close()
            Ledger._migrated.discard(url)
        self.urls.clear()
        for (name,) in self.run("SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'"):
            self.run(f'DROP DATABASE "{name}" WITH (FORCE)')
        for name in self.roles():
            self.run(f'DROP ROLE "{name}"')

    def stop(self) -> None:
        subprocess.run([str(self.server.bin_path / "pg_ctl"), "-D", str(self.pgdata), "-m", "immediate", "stop"],
                       capture_output=True, check=False)
        shutil.rmtree(self.pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def private_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Cluster]:
    c = Cluster(Path(tmp_path_factory.mktemp("pg-rename")) / "data")
    try:
        yield c
    finally:
        c.reset()
        c.stop()


@pytest.fixture(scope="module")
def new_dump(private_cluster: Cluster) -> str:
    """pg_dump of a database this build makes from nothing: what a renamed one has to be equal to."""
    url = private_cluster.database("made_new")
    dbmigrate.migrate(url)
    out = private_cluster.dump(url)
    private_cluster.reset()
    return out


@pytest.fixture
def cluster(private_cluster: Cluster, new_dump: str) -> Iterator[Cluster]:
    yield private_cluster
    private_cluster.reset()


# Every piece of text the catalog holds about the ledger's schemas (old or new names), plus the cluster's roles and
# stored settings. Row data is not part of it.
_CATALOG = """
WITH ns AS (SELECT oid, nspname, nspacl FROM pg_namespace
            WHERE nspname IN ('benchbook', 'benchbook_q', 'assay', 'assay_q')),
rel AS (SELECT c.* FROM pg_class c WHERE c.relnamespace IN (SELECT oid FROM ns))
SELECT 'schema', nspname || ' ' || coalesce(nspacl::text, '') FROM ns
UNION ALL SELECT 'relation', oid::regclass::text || ' ' || coalesce(relacl::text, '') FROM rel
UNION ALL SELECT 'column', a.attrelid::regclass::text || '.' || a.attname || ' ' || coalesce(a.attacl::text, '')
  FROM pg_attribute a JOIN rel ON rel.oid = a.attrelid WHERE a.attnum > 0
UNION ALL SELECT 'function', p.oid::regprocedure::text || ' ' || p.prosrc || ' ' || coalesce(p.proacl::text, '')
  || ' ' || coalesce(array_to_string(p.proconfig, ','), '') FROM pg_proc p WHERE p.pronamespace IN (SELECT oid FROM ns)
UNION ALL SELECT 'view', pg_get_viewdef(oid) FROM rel WHERE relkind IN ('v', 'm')
UNION ALL SELECT 'default', pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d JOIN rel ON rel.oid = d.adrelid
UNION ALL SELECT 'constraint', c.conname || ' ' || pg_get_constraintdef(c.oid)
  FROM pg_constraint c WHERE c.connamespace IN (SELECT oid FROM ns)
UNION ALL SELECT 'trigger', pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN rel ON rel.oid = t.tgrelid
  WHERE NOT t.tgisinternal
UNION ALL SELECT 'index', pg_get_indexdef(i.indexrelid) FROM pg_index i JOIN rel ON rel.oid = i.indrelid
UNION ALL SELECT 'policy', p.polname || ' ' || coalesce(pg_get_expr(p.polqual, p.polrelid), '') || ' ' ||
  coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') FROM pg_policy p JOIN rel ON rel.oid = p.polrelid
UNION ALL SELECT 'type', t.typname FROM pg_type t WHERE t.typnamespace IN (SELECT oid FROM ns)
UNION ALL SELECT 'comment', d.description FROM pg_description d WHERE d.objoid >= 16384
UNION ALL SELECT 'role', rolname FROM pg_roles
UNION ALL SELECT 'setting', array_to_string(setconfig, ', ') FROM pg_db_role_setting
"""


def catalog_mentions(url: str, word: str = "assay") -> list[tuple[str, str]]:
    with psycopg.connect(url) as c:
        c.execute("SET search_path = pg_catalog")  # regclass and the pg_get_*def functions then print full names
        return [(kind, text) for kind, text in c.execute(_CATALOG) if word in text.lower()]


def populate(db_url: str) -> str:
    """A project with every kind of record, written by the SDK."""
    pid = make_project(db_url)
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    worker = Ledger.open(project=pid, agent="worker-1", role="worker", database_url=db_url)
    other = lead.as_agent("worker-2")
    first, _later = lead.create_tasks([{"title": "sweep a", "tags": ["a"]}, {"title": "later", "priority": -1}])
    task = worker.next_task()
    assert task is not None and task.id == first
    uri = worker.put_blob(b"loss curve\n", "text/plain")
    e = worker.record_experiment({"a": 1}, {"score": 1.0}, seed=0, task_id=first, artifacts=[{"uri": uri}])
    other.record_experiment({"a": 1}, {"score": 1.2}, seed=1)
    worker.record_experiment({"a": 2}, {}, run_status="error", notes="boom")
    assert worker.record_experiment({"a": 1}, {"score": 1.0}, seed=0).duplicate_of == e.id  # a dedup_hits row
    c = worker.propose_claim("hypothesis", "a=1 is good", evidence=[(e.id, "supports")])  # type: ignore[list-item]
    worker.add_source("https://example.org/paper", title="paper")
    lead.decide(c.id, "supported", oracle="metric", rationale="measured", evidence_ids=[e.id])  # type: ignore[list-item]
    worker.complete_task(first, "done", "ok")
    worker.check_tried({"a": 1})
    worker.brief()
    lead.update_settings({"rerun_min_seeds": 2})
    return pid


def _shown(row: dict[str, Any], cols: set[str]) -> str:
    return json.dumps({k: v for k, v in row.items() if k in cols}, sort_keys=True)


def test_legacy_sql_is_what_the_old_releases_ran() -> None:
    assert LEGACY_REVISIONS == ["0001", "0002", "0003", "0004", "0005", "0006", "0007"]
    text = {name[:4]: legacy_sql(name) for name in LEGACY_FILES}
    assert not any("benchbook" in t.lower() for t in text.values())
    assert "CREATE SCHEMA IF NOT EXISTS assay;" in text["0001"] and "CREATE ROLE assay_reader LOGIN" in text["0003"]
    assert "current_setting(''assay.project'')" in text["0003"] and "current_setting(''assay.project'')" in text["0005"]
    assert "octet_length(NEW.project_id) <= 57 THEN 'assay_' || NEW.project_id" in text["0004"]


def test_rename_keeps_the_data_and_equals_a_new_database(cluster: Cluster, new_dump: str, db_url: str) -> None:
    pid = populate(db_url)
    url = cluster.legacy()
    copy_project_as_legacy(db_url, pid, url)
    before = table_rows(url, "assay", pid)
    assert all(before[t] for t in TABLES), {t: len(before[t]) for t in TABLES}
    written = table_rows(db_url, "benchbook", pid)  # what the SDK wrote, with the blob prefix of its day
    for t in TABLES:  # the columns a 0007 schema has (later migrations add some), serial ids left out
        cols = set(before[t][0]) - ({"id"} if t in SERIAL else set())
        assert sorted(_shown(r, cols) for r in before[t]) == sorted(
            _shown(r, cols).replace(BLOB_PREFIX, "assay://blob/") for r in written[t]), t
    old_names = catalog_mentions(url)
    assert {kind for kind, _ in old_names} >= {"schema", "relation", "function", "view", "trigger", "index", "role",
                                               "default", "constraint"}
    assert cluster.renamed_roles() == {"assay_reader"}
    assert dbmigrate.status(url) == dbmigrate.SchemaStatus(None, True, "0007")
    assert dbmigrate.current(url) is None and not schema_version_ok(url)  # "needs the rename", not "up to date"

    assert dbmigrate.migrate(url) == ("0007", dbmigrate.head())

    assert dbmigrate.status(url) == dbmigrate.SchemaStatus(dbmigrate.head()) and schema_version_ok(url)
    after = table_rows(url, "benchbook", pid)  # row data is not rewritten...
    assert {t: [{k: r[k] for k in rows[0]} for r in after[t]] for t, rows in before.items()} == before
    assert "assay://blob/" in json.dumps(before["experiments"])  # ...so old rows keep their old blob URIs
    assert catalog_mentions(url) == []
    assert cluster.renamed_roles() == {"benchbook_reader"}
    dump = cluster.dump(url)
    assert "assay" not in dump.lower()
    assert dump == new_dump  # tables, indexes, triggers, function bodies, views and grants, as pg_dump sees them
    with psycopg.connect(url) as c:
        bodies = dict(c.execute("SELECT proname, prosrc FROM pg_proc WHERE pronamespace = 'benchbook'::regnamespace"))
        views = [r[0] for r in c.execute(
            "SELECT pg_get_viewdef(c.oid) FROM pg_class c WHERE c.relnamespace = 'benchbook_q'::regnamespace")]
    assert set(bodies) >= {"forbid_mutation", "check_decider_role", "notify_insert", "forbid_agent_change"}
    assert set(bodies) == set(dbmigrate.function_definitions())
    assert not any("assay" in body.lower() for body in bodies.values())
    assert "<= 53 THEN 'benchbook_' || NEW.project_id" in bodies["notify_insert"]
    assert "FROM benchbook.agents" in bodies["check_decider_role"]
    assert len(views) >= 14 and all("current_setting('benchbook.project'::text)" in v for v in views)


def test_ledger_works_on_a_renamed_database(cluster: Cluster, db_url: str) -> None:
    pid = populate(db_url)
    url = cluster.legacy()
    copy_project_as_legacy(db_url, pid, url)
    dbmigrate.migrate(url)
    lead = Ledger.open(project=pid, agent="lead", database_url=url)
    worker = Ledger.open(project=pid, agent="worker-1", database_url=url)
    assert worker.role == "worker" and lead.role == "orchestrator"  # the agents written before the rename

    old = lead.sql_internal("SELECT id FROM benchbook.experiments WHERE config = '{\"a\": 1}' AND seed = 0")[0]["id"]
    assert worker.record_experiment({"a": 1}, {"score": 1.0}, seed=0).duplicate_of == old
    with psycopg.connect(url, autocommit=True) as listener:
        listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(notification_channel(pid))))
        new = worker.record_experiment({"a": 3}, {"score": 2.0}, seed=0)
        note = next(listener.notifies(timeout=5, stop_after=1), None)
    assert new.outcome == "improved" and note is not None and str(new.id) in note.payload

    # sql() logs in as the reader role (no SET ROLE fallback here) and sees old and new rows through benchbook_q
    tried = lead.sql("SELECT attempts, seeds FROM v_tried ORDER BY attempts DESC")["rows"]
    assert tried[0] == {"attempts": 2, "seeds": 2}
    claim = lead.sql("SELECT id, status, n_support FROM v_claim_status")["rows"]
    assert [(c["status"], c["n_support"]) for c in claim] == [("supported", 1)]
    assert lead.sql("SELECT count(*) AS n FROM v_leaderboard")["rows"] == [{"n": 3}]
    with pytest.raises(BenchbookError, match="permission denied"):
        lead.sql("SELECT * FROM benchbook.experiments")

    # decisions: the role trigger reads benchbook.agents now, and the messages carry the new name
    with pytest.raises(PermissionDenied):
        worker.decide(claim[0]["id"], "retracted", oracle="human", rationale="no")
    with psycopg.connect(url) as c, pytest.raises(psycopg.errors.InsufficientPrivilege, match="benchbook: agent"):
        c.execute("INSERT INTO benchbook.decisions(project_id, target_type, target_id, new_status, oracle, rationale,"
                  " decided_by) VALUES (%s, 'claim', %s, 'retracted', 'human', 'r', 'worker-1')", (pid, claim[0]["id"]))
    decided = lead.decide(claim[0]["id"], "retracted", oracle="human", rationale="after the rename")
    assert decided.new_status == "retracted"
    with psycopg.connect(url) as c, pytest.raises(
            psycopg.errors.RestrictViolation, match=r"benchbook: UPDATE on benchbook\.experiments is forbidden"):
        c.execute("UPDATE benchbook.experiments SET notes = 'x'")

    # blobs stored before the rename are still served; the old row still names them assay://blob/<sha256>
    stored = lead.sql_internal("SELECT artifacts FROM benchbook.experiments WHERE id = %s", (old,))
    uri = stored[0]["artifacts"][0]["uri"]
    assert uri.startswith("assay://blob/")
    assert worker.get_blob(BLOB_PREFIX + uri.rsplit("/", 1)[1]) == b"loss curve\n"

    # the notify trigger function is the new one: a 54-byte id used to get assay_<id>, now it gets the hashed channel
    long_id = (uuid.uuid4().hex * 2)[:54]
    Ledger.init_project(long_id, database_url=url)
    w = Ledger.open(project=long_id, agent="worker", database_url=url)
    with psycopg.connect(url, autocommit=True) as listener:
        assert notification_channel(long_id).startswith("benchbook_:")
        listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(notification_channel(long_id))))
        recorded = w.record_experiment({"valid": True})
        note = next(listener.notifies(timeout=5, stop_after=1), None)
    assert note is not None and str(recorded.id) in note.payload


@pytest.mark.parametrize("revision", LEGACY_REVISIONS)
def test_rename_from_every_revision_then_the_remaining_migrations(cluster: Cluster, new_dump: str,
                                                                  revision: str) -> None:
    # a database older than 0007: the remaining 0001-0007 files (and anything newer) run under the new names
    url = cluster.legacy(revision=revision)
    assert dbmigrate.migrate(url) == (revision, dbmigrate.head())
    assert cluster.dump(url) == new_dump
    assert catalog_mentions(url) == [] and cluster.renamed_roles() == {"benchbook_reader"}
    Ledger.init_project("p", metric="score", direction="max", database_url=url)
    lead = Ledger.open(project="p", agent="lead", database_url=url)
    lead.record_experiment({"a": 1}, {"score": 1.0})
    assert lead.sql("SELECT count(*) AS n FROM v_tried")["rows"] == [{"n": 1}]


def test_function_bodies_come_from_the_sql_files(cluster: Cluster) -> None:
    from alembic import command

    url = cluster.database("at_legacy_head")
    command.upgrade(dbmigrate.alembic_config(url), dbmigrate.LEGACY_HEAD)
    in_database = {r[0] for r in cluster.run(
        "SELECT proname FROM pg_proc WHERE pronamespace = 'benchbook'::regnamespace", url)}
    # every function a pre-rename database can have is found in the files, so none keeps an old body
    assert set(dbmigrate.function_definitions(dbmigrate.LEGACY_HEAD)) == in_database
    assert set(dbmigrate.function_definitions("0005")) == in_database - {"forbid_agent_change"}  # added by 0006
    assert "octet_length" not in dbmigrate.function_definitions("0003")["notify_insert"]  # replaced by 0004
    assert "octet_length" in dbmigrate.function_definitions("0004")["notify_insert"]
    assert all(s.startswith("CREATE OR REPLACE FUNCTION ") and s.rstrip().endswith("$$;")
               for s in dbmigrate.function_definitions().values())


def test_migrate_twice_and_from_two_connections_at_once(cluster: Cluster, new_dump: str) -> None:
    # Two processes (Alembic's context is process-global, so threads would not be two independent starts).
    url = cluster.legacy()
    head = dbmigrate.head()
    code = "import json, sys; from benchbook.db.migrate import migrate; print(json.dumps(migrate(sys.argv[1])))"
    with psycopg.connect(url, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (dbmigrate.LOCK_KEY,))  # as if a migration were running
        procs = [subprocess.Popen([sys.executable, "-c", code, url], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, cwd=ROOT) for _ in range(2)]
        waiting = 0
        for _ in range(600):  # until both have seen the old schema and queue for the lock
            row = holder.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted").fetchone()
            waiting = row[0] if row else 0
            if waiting == 2 or any(p.poll() is not None for p in procs):
                break
            time.sleep(0.05)
        assert waiting == 2, [p.communicate() for p in procs if p.poll() is not None]
        holder.execute("SELECT pg_advisory_unlock(%s)", (dbmigrate.LOCK_KEY,))
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    results = [tuple(json.loads(out)) for out, _ in outs]
    # the first one in renamed it; the other took the lock afterwards and found it renamed (and maybe migrated)
    assert all(r in (("0007", head), (head, head)) for r in results), results
    assert ("0007", head) in results
    assert dbmigrate.migrate(url) == (head, head)  # and once more
    assert cluster.dump(url) == new_dump and cluster.renamed_roles() == {"benchbook_reader"}


def test_both_schemas_are_refused(cluster: Cluster) -> None:
    # 1. an old binary connected after the rename and built its schema again, next to the real ledger
    url = cluster.database("both")
    dbmigrate.migrate(url)
    Ledger.init_project("kept", database_url=url)
    build_legacy(url)
    before = cluster.dump(url)
    with pytest.raises(dbmigrate.LegacySchemaConflict) as ei:
        dbmigrate.migrate(url)
    msg = str(ei.value)
    assert "assay (revision 0007, 0 projects)" in msg and f"benchbook (revision {dbmigrate.head()}, 1 project)" in msg
    assert "will not guess" in msg and "DROP SCHEMA assay CASCADE; DROP SCHEMA IF EXISTS assay_q CASCADE;" in msg
    assert cluster.dump(url) == before and dbmigrate.current(url) == dbmigrate.head()
    cluster.run("DROP SCHEMA assay CASCADE; DROP SCHEMA IF EXISTS assay_q CASCADE;", url)  # what the message says
    assert dbmigrate.migrate(url) == (dbmigrate.head(), dbmigrate.head())

    # 2. a pre-rename ledger and a schema called benchbook that is not a ledger
    url = cluster.legacy("stray")
    cluster.run("CREATE SCHEMA benchbook", url)
    before = cluster.dump(url)
    with pytest.raises(dbmigrate.LegacySchemaConflict, match=r"benchbook \(revision none, no projects table\)"):
        dbmigrate.migrate(url)
    assert cluster.dump(url) == before

    # 3. Alembic itself never builds a second ledger next to a pre-rename one
    from alembic import command

    url = cluster.legacy("direct")
    with pytest.raises(dbmigrate.LegacySchemaConflict):
        command.upgrade(dbmigrate.alembic_config(url), "head")
    assert cluster.run("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'benchbook%'", url) == []


def test_unknown_legacy_revision_is_refused(cluster: Cluster) -> None:
    url = cluster.legacy()
    cluster.run("UPDATE assay.alembic_version SET version_num = '9999'", url)
    before = cluster.dump(url)
    with pytest.raises(BenchbookError, match="revision 9999, which no release under that name produced"):
        dbmigrate.migrate(url)
    assert cluster.dump(url) == before and cluster.renamed_roles() == {"assay_reader"}


def test_failure_midway_changes_nothing(cluster: Cluster, new_dump: str, monkeypatch: pytest.MonkeyPatch) -> None:
    url = cluster.legacy()
    cluster.run("INSERT INTO assay.projects(id, title, goal) VALUES ('p', 't', 'g')", url)
    before = cluster.dump(url)

    # 1. a crash after the role and both schemas were renamed and the functions replaced
    def boom(conn: Any) -> None:
        assert conn.execute("SELECT count(*) FROM benchbook.projects").fetchone() == (1,)  # really was midway
        assert conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'benchbook_reader'").fetchone() == (1,)
        raise RuntimeError("power cut")

    with monkeypatch.context() as m:
        m.setattr(dbmigrate, "_repoint_views", boom)
        with pytest.raises(RuntimeError, match="power cut"):
            dbmigrate.migrate(url)
    assert cluster.dump(url) == before and cluster.renamed_roles() == {"assay_reader"}
    assert dbmigrate.status(url) == dbmigrate.SchemaStatus(None, True, "0007")
    with psycopg.connect(url, autocommit=True) as c:  # still a working pre-rename ledger
        with pytest.raises(psycopg.errors.RestrictViolation, match=r"assay: TRUNCATE on assay\.\w+ is forbidden"):
            c.execute("TRUNCATE assay.projects CASCADE")
        c.execute("SET search_path = assay_q")
        c.execute("SET assay.project = 'p'")
        c.execute("SET ROLE assay_reader")
        assert c.execute("SELECT count(*) FROM v_agent_stats").fetchone() == (0,)

    # 2. something benchbook did not create and cannot rewrite: refused with its name, and again nothing changes
    cluster.run("CREATE FUNCTION assay.custom() RETURNS bigint LANGUAGE sql AS 'SELECT count(*) FROM assay.projects';"
                "CREATE VIEW assay.custom_view AS SELECT current_setting('assay.other') AS x", url)
    before = cluster.dump(url)
    with pytest.raises(BenchbookError) as ei:
        dbmigrate.migrate(url)
    assert "- function benchbook.custom()" in str(ei.value) and "- view benchbook.custom_view" in str(ei.value)
    assert "nothing was changed" in str(ei.value)
    assert cluster.dump(url) == before and cluster.renamed_roles() == {"assay_reader"}

    cluster.run("DROP FUNCTION assay.custom(); DROP VIEW assay.custom_view", url)
    assert dbmigrate.migrate(url) == ("0007", dbmigrate.head())
    assert cluster.dump(url) == new_dump


# Roles are cluster-wide ------------------------------------------------------------------------------------------

def test_role_rename_carries_the_other_databases_of_the_cluster(cluster: Cluster, new_dump: str,
                                                                caplog: pytest.LogCaptureFixture) -> None:
    first, second = cluster.legacy("first"), cluster.legacy("second")
    with caplog.at_level(logging.WARNING, logger="benchbook.db.migrate"):
        dbmigrate.migrate(first)
    # pg_shdepend showed that another database still uses the role, and the rename said so
    assert "renamed to benchbook_reader for the whole cluster" in caplog.text and ": second." in caplog.text
    assert cluster.renamed_roles() == {"benchbook_reader"}
    assert dbmigrate.status(second) == dbmigrate.SchemaStatus(None, True, "0007")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="benchbook.db.migrate"):
        assert dbmigrate.migrate(second) == ("0007", dbmigrate.head())  # only the new role exists now
    assert caplog.text == ""
    for url in (first, second):
        assert cluster.dump(url) == new_dump and catalog_mentions(url) == []
        Ledger.init_project("p", database_url=url)
        assert Ledger.open(project="p", agent="lead", database_url=url).sql(
            "SELECT count(*) AS n FROM v_agent_stats")["rows"] == [{"n": 3}]


def test_new_role_exists_already(cluster: Cluster, new_dump: str, caplog: pytest.LogCaptureFixture) -> None:
    # a database made by this build came first, so the cluster has both roles
    dbmigrate.migrate(cluster.database("made_new_first"))
    first, second = cluster.legacy("first"), cluster.legacy("second")
    assert cluster.renamed_roles() == {"assay_reader", "benchbook_reader"}
    with caplog.at_level(logging.WARNING, logger="benchbook.db.migrate"):
        dbmigrate.migrate(first)
    assert "assay_reader is still used by: second" in caplog.text
    assert cluster.dump(first) == new_dump
    assert catalog_mentions(first) == [("role", "assay_reader")]  # the role is there for `second`, unused here
    assert cluster.run("SELECT has_schema_privilege('assay_reader', 'benchbook_q', 'USAGE'),"
                       " has_schema_privilege('benchbook_reader', 'benchbook_q', 'USAGE'),"
                       " has_table_privilege('assay_reader', 'benchbook_q.v_tried', 'SELECT'),"
                       " has_table_privilege('benchbook_reader', 'benchbook_q.v_tried', 'SELECT')", first
                       ) == [(False, True, False, True)]
    Ledger.init_project("p", database_url=first)
    assert Ledger.open(project="p", agent="lead", database_url=first).sql(
        "SELECT count(*) AS n FROM v_agent_stats")["rows"] == [{"n": 3}]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="benchbook.db.migrate"):
        dbmigrate.migrate(second)
    assert "no database in this cluster uses assay_reader any more (DROP ROLE assay_reader; removes it)" in caplog.text
    assert cluster.dump(second) == new_dump
    cluster.run("DROP ROLE assay_reader")  # nothing depends on it: Postgres would refuse otherwise
    assert cluster.renamed_roles() == {"benchbook_reader"}


def test_missing_role_is_created(cluster: Cluster, new_dump: str) -> None:
    url = cluster.legacy()
    cluster.run("DROP OWNED BY assay_reader; DROP ROLE assay_reader", url)
    assert dbmigrate.migrate(url) == ("0007", dbmigrate.head())
    assert cluster.renamed_roles() == {"benchbook_reader"} and cluster.dump(url) == new_dump


def _scram_matches(verifier: str, password: str) -> bool:
    m = re.fullmatch(r"SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)", verifier)
    assert m, verifier
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(m[2]), int(m[1]))
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    return base64.b64encode(hashlib.sha256(client_key).digest()).decode() == m[3]


def _reader_password(cluster: Cluster) -> str | None:
    return cluster.run("SELECT rolpassword FROM pg_authid WHERE rolname = 'benchbook_reader'")[0][0]  # type: ignore[no-any-return]


def test_reader_password_survives_the_rename(cluster: Cluster, monkeypatch: pytest.MonkeyPatch,
                                             caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.delenv("BENCHBOOK_READER_PASSWORD", raising=False)
    monkeypatch.delenv("ASSAY_READER_PASSWORD", raising=False)
    # SCRAM (the default since Postgres 14): the hash does not involve the role name, the rename keeps it
    url = cluster.legacy()
    cluster.run("ALTER ROLE assay_reader PASSWORD 'read-me'")
    with caplog.at_level(logging.WARNING, logger="benchbook.db.migrate"):
        dbmigrate.migrate(url)
    assert _scram_matches(_reader_password(cluster) or "", "read-me") and caplog.text == ""
    cluster.reset()

    # MD5: Postgres clears it. Without the password in the environment the administrator is told what to run
    url = cluster.legacy()
    cluster.run("SET password_encryption = 'md5'; ALTER ROLE assay_reader PASSWORD 'read-me'")
    assert cluster.run("SELECT rolpassword LIKE 'md5%' FROM pg_authid WHERE rolname = 'assay_reader'") == [(True,)]
    with caplog.at_level(logging.WARNING, logger="benchbook.db.migrate"):
        dbmigrate.migrate(url)
    assert _reader_password(cluster) is None
    assert "ALTER ROLE benchbook_reader PASSWORD '...';" in caplog.text
    cluster.reset()
    caplog.clear()

    # MD5 with BENCHBOOK_READER_PASSWORD set (what every machine logs in with): the reader keeps that password
    url = cluster.legacy()
    cluster.run("SET password_encryption = 'md5'; ALTER ROLE assay_reader PASSWORD 'read-me'")
    monkeypatch.setenv("BENCHBOOK_READER_PASSWORD", "read-me")
    with caplog.at_level(logging.WARNING, logger="benchbook.db.migrate"):
        dbmigrate.migrate(url)
    assert _scram_matches(_reader_password(cluster) or "", "read-me")
    assert "got the password from BENCHBOOK_READER_PASSWORD again" in caplog.text and "read-me" not in caplog.text


def test_without_the_privilege_nothing_changes_and_the_sql_is_printed(cluster: Cluster) -> None:
    # a managed Postgres: the ledger's owner is an ordinary role that may not rename roles
    cluster.run("CREATE ROLE bb_owner LOGIN; CREATE ROLE assay_reader LOGIN;")
    cluster.run('CREATE DATABASE "owned" OWNER bb_owner')
    admin_url = cluster.database("owned")
    url = admin_url.replace("//postgres:@", "//bb_owner:@")
    assert url != admin_url
    build_legacy(url)
    owner = cluster.run("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'assay'", url)
    assert owner == [("bb_owner",)]
    cluster.run("ALTER ROLE assay_reader SET search_path = assay_q, public")  # an administrator's, also refused
    before = cluster.dump(admin_url)

    with pytest.raises(BenchbookError) as ei:
        dbmigrate.migrate(url)
    msg = str(ei.value)
    assert "bb_owner may not run" in msg and "Nothing was changed" in msg
    todo = re.findall(r"^  (.+;)$", msg, re.M)
    # all of it, in the order it has to run: the setting names the role as renamed by the line before
    assert todo == ['ALTER ROLE "assay_reader" RENAME TO "benchbook_reader";',
                    'ALTER ROLE "benchbook_reader" SET search_path = "benchbook_q", "public";']
    assert cluster.dump(admin_url) == before and cluster.renamed_roles() == {"assay_reader", "bb_owner"}
    assert dbmigrate.status(url) == dbmigrate.SchemaStatus(None, True, "0007")

    cluster.run("\n".join(todo))  # the administrator runs exactly what the message printed
    # the rename is done; a later migration (0008) needs a role only an administrator can create, and says so
    with pytest.raises(BenchbookError, match="migration stopped at revision 0007: .*bb_owner may not create") as ei:
        dbmigrate.migrate(url)
    todo = re.findall(r"(CREATE ROLE \w+ LOGIN;)", str(ei.value))
    assert todo == ["CREATE ROLE benchbook_writer LOGIN;"]
    assert dbmigrate.status(url) == dbmigrate.SchemaStatus("0007")
    cluster.run(todo[0])
    assert dbmigrate.migrate(url) == ("0007", dbmigrate.head())
    assert catalog_mentions(admin_url) == [] and cluster.renamed_roles() == {"benchbook_reader", "bb_owner"}
    Ledger.init_project("p", database_url=url)
    cluster.urls.append(url)
    assert Ledger.open(project="p", agent="lead", database_url=url).sql(
        "SELECT count(*) AS n FROM v_agent_stats")["rows"] == [{"n": 3}]


def test_stored_settings_are_rewritten(cluster: Cluster) -> None:
    # benchbook stores none of these; an administrator may have, for a login that reads the views directly
    url = cluster.legacy("configured")
    cluster.run("ALTER ROLE assay_reader SET search_path = assay_q, public;"
                "ALTER ROLE assay_reader SET assay.project = 'demo';"
                'ALTER DATABASE configured SET search_path = "$user", assay, public;'
                "ALTER ROLE assay_reader IN DATABASE configured SET statement_timeout = '5s'")
    dbmigrate.migrate(url)
    settings = {(db, role): sorted(config) for db, role, config in cluster.run(
        "SELECT d.datname, r.rolname, s.setconfig FROM pg_db_role_setting s"
        " LEFT JOIN pg_database d ON d.oid = s.setdatabase LEFT JOIN pg_roles r ON r.oid = s.setrole")}
    assert settings == {
        (None, "benchbook_reader"): ["benchbook.project=demo", "search_path=benchbook_q, public"],
        ("configured", None): ['search_path="$user", benchbook, public'],
        ("configured", "benchbook_reader"): ["statement_timeout=5s"],
    }
    assert catalog_mentions(url) == []
    with psycopg.connect(url, user="benchbook_reader") as c:  # the login works as the administrator set it up
        assert c.execute("SELECT count(*) FROM v_agent_stats").fetchone() == (0,)


# The command line and the invariant suite ------------------------------------------------------------------------

def test_cli_migrate_and_doctor_name_the_rename(cluster: Cluster, tmp_path: Path) -> None:
    url = cluster.legacy()
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": url, "HOME": str(tmp_path / "home"),
           "XDG_STATE_HOME": str(tmp_path / "state"), "CLAUDE_CONFIG_DIR": str(tmp_path / "home" / ".claude")}
    for name in ("BENCHBOOK_PROJECT", "BENCHBOOK_DATA_DIR"):
        env.pop(name, None)

    def cli(*args: str) -> dict[str, Any]:
        p = subprocess.run([sys.executable, "-m", "benchbook.cli", "--json", *args], capture_output=True, text=True,
                           cwd=tmp_path, env=env, timeout=120)
        return json.loads(p.stdout)  # type: ignore[no-any-return]

    checks = {c["check"]: c for c in cli("doctor")["checks"]}
    assert checks["db schema"] == {"check": "db schema", "ok": False, "detail": "pre-rename schema assay at 0007",
                                   "fix": "benchbook migrate"}
    assert cli("migrate") == {"ok": True, "from": "0007", "to": dbmigrate.head(), "renamed_from": "assay"}
    assert cli("migrate") == {"ok": True, "from": dbmigrate.head(), "to": dbmigrate.head()}
    checks = {c["check"]: c for c in cli("doctor")["checks"]}
    assert checks["db schema"]["ok"] and checks["benchbook_reader role"]["ok"]


def test_session_database_origin(db_url: str) -> None:
    """Under BENCHBOOK_TEST_LEGACY_DB the session database was built with the old names and renamed (it still has the
    row written before the rename); otherwise it was made new."""
    with psycopg.connect(db_url) as c:
        marked = c.execute("SELECT 1 FROM benchbook.projects WHERE id = %s", (MARKER_PROJECT,)).fetchone() is not None
        assert c.execute("SELECT 1 FROM pg_namespace WHERE nspname IN ('assay', 'assay_q')").fetchone() is None
    assert marked == bool(os.environ.get(LEGACY_SWITCH))


@pytest.mark.skipif(bool(os.environ.get(LEGACY_SWITCH)), reason="this whole run is on a renamed database already")
def test_invariant_suite_passes_on_a_renamed_database() -> None:
    """tests/test_invariants.py, triggers included, in a pytest run whose database came through the rename."""
    env = {**os.environ, LEGACY_SWITCH: "1"}
    env.pop("BENCHBOOK_DATABASE_URL", None)  # the child makes its own database
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_invariants.py",
         "tests/test_db.py::test_session_database_origin"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=1200)
    assert p.returncode == 0, p.stdout[-6000:] + p.stderr[-2000:]
    passed = re.search(r"(\d+) passed", p.stdout)
    assert passed and int(passed[1]) > 30 and "skipped" not in p.stdout and "failed" not in p.stdout, p.stdout[-2000:]
