"""Schema migrations via Alembic. Each revision executes a raw SQL file from alembic/sql/,
so the schema stays reviewable as plain SQL (spec §16.1: no ORM).

Alembic/SQLAlchemy are imported only when an upgrade is actually needed: the version check is plain
SQL, which keeps CLI and hook start-up fast (the hook must finish within 1s, §12.3).

A database made before the project was renamed (schema assay, reader schema assay_q, role assay_reader, revision 0007
at most) is renamed in place before Alembic looks at it (_rename_legacy). That step has no revision number: it is
keyed on the schema names, runs in one transaction under the migration lock, and needs psycopg only."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from ..config import env
from ..errors import BenchbookError

ALEMBIC_DIR = Path(str(resources.files("benchbook.db").joinpath("alembic")))

# Serialises concurrent migrations (here and in alembic/env.py). The value is the one the pre-rename releases used,
# on purpose: an old binary migrating the same database waits for the same lock.
LOCK_KEY = 0x61737361

SCHEMA, READER_SCHEMA, READER_ROLE = "benchbook", "benchbook_q", "benchbook_reader"
LEGACY_SCHEMA, LEGACY_READER_SCHEMA, LEGACY_READER_ROLE = "assay", "assay_q", "assay_reader"
LEGACY_HEAD = "0007"  # the newest revision that was ever applied under the old names

log = logging.getLogger(__name__)


def revisions() -> list[str]:
    """Revision ids are the NNNN prefix of the version files, in a straight line."""
    return sorted(p.name.split("_", 1)[0] for p in (ALEMBIC_DIR / "versions").glob("[0-9]*_*.py"))


def head() -> str:
    return revisions()[-1]


class SchemaTooNew(BenchbookError):
    """The database was migrated by a newer benchbook than this one."""


class LegacySchemaConflict(BenchbookError):
    """The database holds a pre-rename ledger that cannot be renamed without guessing (both schemas exist)."""


def run_sql_file(name: str) -> None:
    from alembic import op

    text = (ALEMBIC_DIR / "sql" / name).read_text(encoding="utf-8")
    # Run on the raw psycopg connection with no params, so % and : in the SQL are left alone.
    raw: Any = op.get_bind().connection.driver_connection
    raw.execute(text.encode("utf-8"))  # bytes: psycopg sends it as one simple-query script
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


# Schema status ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SchemaStatus:
    """revision: benchbook.alembic_version, None when that table is missing. legacy: a ledger from before the rename
    (assay.alembic_version) is present; legacy_revision is its revision."""
    revision: str | None
    legacy: bool = False
    legacy_revision: str | None = None


_FAST_STATUS = sql.SQL("SELECT (SELECT version_num FROM {} LIMIT 1), to_regclass({}) IS NOT NULL").format(
    sql.Identifier(SCHEMA, "alembic_version"), sql.Literal(f"{LEGACY_SCHEMA}.alembic_version"))


def _version(conn: psycopg.Connection[Any], schema: str) -> str | None:
    row = conn.execute(sql.SQL("SELECT version_num FROM {} LIMIT 1").format(
        sql.Identifier(schema, "alembic_version"))).fetchone()
    return str(row[0]) if row else None


def _has_table(conn: psycopg.Connection[Any], schema: str, table: str = "alembic_version") -> bool:
    """Read from the catalog tables, not with to_regclass(): in a transaction that has just waited for the migration
    lock, to_regclass() can still answer from this backend's cache of before the wait (found by the two-connection
    test), while a catalog query sees what the other process committed."""
    return conn.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'r'", (schema, table)).fetchone() is not None


def _inspect(conn: psycopg.Connection[Any]) -> SchemaStatus:
    """The status without a statement that can fail, so it is usable inside a transaction."""
    new, legacy = _has_table(conn, SCHEMA), _has_table(conn, LEGACY_SCHEMA)
    return SchemaStatus(_version(conn, SCHEMA) if new else None, legacy,
                        _version(conn, LEGACY_SCHEMA) if legacy else None)


def _status(conn: psycopg.Connection[Any]) -> SchemaStatus:
    """One query when the schema is in place (every CLI call and hook pays for it). conn must be in autocommit mode:
    the query fails on a database that has no benchbook schema yet."""
    try:
        row = conn.execute(_FAST_STATUS).fetchone()
    except psycopg.errors.UndefinedTable:
        return _inspect(conn)
    assert row is not None
    if row[1]:
        return _inspect(conn)
    return SchemaStatus(str(row[0]) if row[0] is not None else None)


def status(url: str) -> SchemaStatus:
    with psycopg.connect(url, autocommit=True) as conn:
        return _status(conn)


def current(url: str) -> str | None:
    """The revision of the benchbook schema. None for an empty database, and for one that still has only the
    pre-rename schema: it needs migrate() before anything can use it."""
    return status(url).revision


def migrate(url: str) -> tuple[str | None, str]:
    """Upgrade to head. Returns (before, after). Safe to call concurrently (advisory lock, here and in env.py).
    A pre-rename database is renamed first; before is then the revision it had under the old name."""
    with psycopg.connect(url, autocommit=True) as conn:
        st = _status(conn)
        if st.legacy:
            st = _rename_legacy(conn)
    before = st.revision
    if before is not None and before not in revisions():
        raise SchemaTooNew(f"database schema revision {before} is newer than this benchbook (head {head()}); "
                           "upgrade benchbook on this machine (e.g. `uv tool upgrade benchbook`)")
    if before != head():
        from alembic import command

        try:
            command.upgrade(alembic_config(url), "head")
        except psycopg.errors.InsufficientPrivilege as e:
            # a migration that needs a server-wide privilege (creating a role) says what an administrator must run
            raise BenchbookError(f"migration stopped at revision {current(url)}: {e.diag.message_primary or e}"
                                 + (f"\n{e.diag.message_hint}" if e.diag.message_hint else "")) from e
    return before, head()


# The rename of a pre-rename database -----------------------------------------------------------------------------
#
# ALTER SCHEMA ... RENAME moves tables, indexes, triggers, grants and view references (they follow OIDs). It does not
# touch text: plpgsql bodies, the setting name the reader views filter on, and search_path settings stored for a role
# or database. Those are rewritten here, and _leftovers() refuses to commit while any of them still has the old name.

_CREATE_FUNCTION = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?P<name>[\w.]+)\s*\([^;]*?\bAS\s+(?P<tag>\$\w*\$).*?(?P=tag)[^;]*;",
    re.IGNORECASE | re.DOTALL | re.MULTILINE)


def function_definitions(upto: str | None = None) -> dict[str, str]:
    """name -> the newest CREATE FUNCTION statement in the SQL files of the revisions up to `upto`, as
    CREATE OR REPLACE. Read from the migration files themselves, so a renamed database gets exactly the bodies a new
    one gets and the two cannot drift apart."""
    out: dict[str, str] = {}
    for path in sorted((ALEMBIC_DIR / "sql").glob("[0-9]*_*.sql")):
        if upto is not None and path.name.split("_", 1)[0] > upto:
            break
        for m in _CREATE_FUNCTION.finditer(path.read_text(encoding="utf-8")):
            statement = re.sub(r"^CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION", "CREATE OR REPLACE FUNCTION", m[0],
                               count=1, flags=re.IGNORECASE)
            out[m["name"].rsplit(".", 1)[-1].lower()] = statement
    return out


def _rename_legacy(conn: psycopg.Connection[Any]) -> SchemaStatus:
    notices: list[str] = []

    def on_notice(diag: psycopg.errors.Diagnostic) -> None:
        notices.append(diag.message_primary or "")

    notes: list[str] = []
    conn.add_notice_handler(on_notice)
    try:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            st = _inspect(conn)  # again, under the lock: another process may have renamed it while we waited
            if st.legacy:
                notes = _rename(conn, st, notices)
                st = SchemaStatus(st.legacy_revision)
    finally:
        conn.remove_notice_handler(on_notice)
    for note in notes:  # after the commit, so nothing is announced that was rolled back
        log.warning("%s", note)
    return st


def _rename(conn: psycopg.Connection[Any], st: SchemaStatus, notices: list[str]) -> list[str]:
    """Everything below runs in the caller's transaction: an error leaves the database (and the cluster's roles)
    exactly as it was."""
    present = {str(r[0]) for r in conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)",
        ([SCHEMA, READER_SCHEMA, LEGACY_SCHEMA, LEGACY_READER_SCHEMA],))}
    has_reader = LEGACY_READER_SCHEMA in present  # revision 0003 or later
    if SCHEMA in present or (has_reader and READER_SCHEMA in present):
        raise LegacySchemaConflict(_conflict_message(conn, st, present))
    rev = st.legacy_revision
    if rev is None or rev not in revisions() or rev > LEGACY_HEAD:
        raise BenchbookError(
            f"schema {LEGACY_SCHEMA} is at revision {rev or '(none)'}, which no release under that name produced "
            f"(the last one was {LEGACY_HEAD}); benchbook will not rename it")

    notes: list[str] = []
    old_role, new_role = _role_oid(conn, LEGACY_READER_ROLE), _role_oid(conn, READER_ROLE)
    old, new = sql.Identifier(LEGACY_READER_ROLE), sql.Identifier(READER_ROLE)
    retire: int | None = None
    others: list[str] = []
    rename_role = has_reader and new_role is None and old_role is not None
    admin: list[sql.Composed] = []
    if rename_role and old_role is not None:
        # Roles belong to the cluster. The rename keeps the OID, so the grants of every other database follow it;
        # their own schema is renamed when benchbook first connects to them (they then find only the new role).
        others = _databases_using(conn, old_role)
        admin.append(sql.SQL("ALTER ROLE {} RENAME TO {}").format(old, new))
    elif has_reader and new_role is None:
        admin.append(sql.SQL("CREATE ROLE {} LOGIN").format(new))  # someone dropped the role; 0003 made it
    elif has_reader and old_role is not None:
        retire = old_role  # both exist: a new database was created in this cluster first
    # Everything that may need an administrator comes first, so a refusal stops before any other change and the
    # message can list all of it in the order it has to run (the settings name the role as renamed).
    admin += _setting_statements(conn, {LEGACY_READER_ROLE: READER_ROLE} if rename_role else {})
    seen = len(notices)
    _as_admin(conn, admin, f"If {LEGACY_READER_ROLE} has an MD5 password the rename clears it; set it again with "
                           f"ALTER ROLE {READER_ROLE} PASSWORD '...'." if rename_role else "")
    if rename_role and any("MD5" in n for n in notices[seen:]):  # an MD5 hash is salted with the role name
        notes.append(_keep_reader_password(conn))
    if others:
        notes.append(
            f"role {LEGACY_READER_ROLE} was renamed to {READER_ROLE} for the whole cluster. These databases hold "
            f"grants to it too: {', '.join(others)}. A pre-rename ledger among them is renamed when benchbook first "
            "connects to it; until then a pre-rename binary cannot run its sql command there.")

    conn.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(sql.Identifier(LEGACY_SCHEMA),
                                                                sql.Identifier(SCHEMA)))
    if has_reader:
        conn.execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(sql.Identifier(LEGACY_READER_SCHEMA),
                                                                    sql.Identifier(READER_SCHEMA)))
    _recreate_functions(conn, rev)
    _repoint_views(conn)
    if has_reader:
        _grant_reader(conn, retire is not None)

    left = _leftovers(conn, retire)
    if left:
        raise BenchbookError(
            f"renaming schema {LEGACY_SCHEMA} to {SCHEMA} would leave objects that still use the old name, so "
            "nothing was changed:\n" + "\n".join(f"- {x}" for x in left) + "\n"
            "These are not from benchbook's migrations. Change or drop them, then run `benchbook migrate`.")
    if retire is not None:
        using = _databases_using(conn, retire)
        notes.append(
            f"this database now uses role {READER_ROLE}; " + (
                f"{LEGACY_READER_ROLE} is still used by: {', '.join(using)} (renamed when benchbook first connects "
                "to them)." if using else
                f"no database in this cluster uses {LEGACY_READER_ROLE} any more "
                f"(DROP ROLE {LEGACY_READER_ROLE}; removes it)."))
    log.info("renamed schema %s to %s at revision %s", LEGACY_SCHEMA, SCHEMA, rev)
    return notes


def _conflict_message(conn: psycopg.Connection[Any], st: SchemaStatus, present: set[str]) -> str:
    def describe(schema: str, revision: str | None) -> str:
        n = None
        if _has_table(conn, schema, "projects"):
            row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, "projects"))).fetchone()
            n = row[0] if row else None
        return (f"{schema} (revision {revision or 'none'}, " +
                (f"{n} project{'s' if n != 1 else ''})" if n is not None else "no projects table)"))

    both = [describe(LEGACY_SCHEMA, st.legacy_revision)]
    if SCHEMA in present:
        both.append(describe(SCHEMA, st.revision))
    else:
        both.append(f"{READER_SCHEMA} (next to {LEGACY_READER_SCHEMA})")
    return (
        f"this database has both the pre-rename schema and the new one: {' and '.join(both)}. benchbook will not "
        "guess which is the ledger. Keep the one with your data and drop the other as the database owner, then "
        f"run `benchbook migrate`: DROP SCHEMA {LEGACY_SCHEMA} CASCADE; DROP SCHEMA IF EXISTS "
        f"{LEGACY_READER_SCHEMA} CASCADE;  -- when {LEGACY_SCHEMA} is the leftover (for example a pre-rename "
        f"binary connected after the rename), or DROP SCHEMA IF EXISTS {READER_SCHEMA} CASCADE; DROP SCHEMA "
        f"{SCHEMA} CASCADE;  -- when {SCHEMA} is")


def _role_oid(conn: psycopg.Connection[Any], name: str) -> int | None:
    row = conn.execute("SELECT oid FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
    return int(row[0]) if row else None


def _databases_using(conn: psycopg.Connection[Any], role: int) -> list[str]:
    """Other databases of the cluster that hold grants to (or objects of) the role, from the shared catalog."""
    return [str(r[0]) for r in conn.execute(
        "SELECT DISTINCT d.datname FROM pg_shdepend s JOIN pg_database d ON d.oid = s.dbid"
        " WHERE s.refclassid = 'pg_authid'::regclass AND s.refobjid = %s AND d.datname <> current_database()"
        " ORDER BY 1", (role,))]


def _as_admin(conn: psycopg.Connection[Any], statements: list[sql.Composed], hint: str = "") -> None:
    """Statements about roles and stored settings can need more than ownership of the ledger. When one is refused,
    stop (the caller's transaction rolls back, the statements that did run included) and print all of them for an
    administrator to run."""
    for statement in statements:
        try:
            with conn.transaction():
                conn.execute(statement)
        except psycopg.errors.InsufficientPrivilege as e:
            todo = "\n".join(f"  {s.as_string(conn)};" for s in statements)
            user = conn.execute("SELECT current_user").fetchone()
            raise BenchbookError(
                f"renaming the pre-rename schema {LEGACY_SCHEMA} to {SCHEMA} needs a statement that "
                f"{user[0] if user else 'this user'} may not run ({(e.diag.message_primary or str(e)).strip()}). "
                "Nothing was changed. A database administrator (superuser, or CREATEROLE with ADMIN on the role) "
                f"must run:\n{todo}\nand then `benchbook migrate` again." + (f" {hint}" if hint else "")) from e


def _keep_reader_password(conn: psycopg.Connection[Any]) -> str:
    """The rename cleared the reader's MD5 password. BENCHBOOK_READER_PASSWORD is what every machine logs in with, so
    set it again under the new name (hashed here, the way the server is configured, never sent as plain text)."""
    password = env("READER_PASSWORD")
    if not password:
        return (f"role {LEGACY_READER_ROLE} had an MD5 password, which Postgres clears when a role is renamed. "
                f"Set it again, or the sql command cannot log in: ALTER ROLE {READER_ROLE} PASSWORD '...';")
    verifier = conn.pgconn.encrypt_password(password.encode("utf-8"), READER_ROLE.encode("utf-8")).decode("ascii")
    conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(READER_ROLE), sql.Literal(verifier)))
    return (f"role {LEGACY_READER_ROLE} had an MD5 password, which Postgres clears when a role is renamed; "
            f"{READER_ROLE} got the password from BENCHBOOK_READER_PASSWORD again.")


def _recreate_functions(conn: psycopg.Connection[Any], revision: str) -> None:
    existing = {str(r[0]) for r in conn.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s",
        (SCHEMA,))}
    conn.execute(sql.SQL("SET LOCAL search_path = {}").format(sql.Identifier(SCHEMA)))  # as the SQL files do
    for name, statement in function_definitions(revision).items():
        if name in existing:  # CREATE OR REPLACE keeps the OID, so every trigger stays attached
            conn.execute(statement.encode("utf-8"))


def _repoint_views(conn: psycopg.Connection[Any]) -> None:
    """The per-project reader views filter on current_setting('assay.project'); the SDK now sets benchbook.project."""
    conn.execute("SET LOCAL search_path = pg_catalog")  # so pg_get_viewdef qualifies every name it prints
    old, new = f"'{LEGACY_SCHEMA}.project'", f"'{SCHEMA}.project'"
    views = conn.execute(
        "SELECT n.nspname, c.relname, pg_get_viewdef(c.oid) FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = ANY(%s) AND c.relkind = 'v'",
        ([SCHEMA, READER_SCHEMA],)).fetchall()
    for schema, name, definition in views:
        if old in definition:  # OR REPLACE keeps the view's OID and its grants
            head_ = sql.SQL("CREATE OR REPLACE VIEW {} AS ").format(sql.Identifier(schema, name))
            conn.execute(head_.as_bytes(conn) + definition.replace(old, new).encode("utf-8"))


def _grant_reader(conn: psycopg.Connection[Any], retire_old: bool) -> None:
    """What 0003 and 0005 grant. After a role rename the grants are already there; when the new role came from
    another database they are not, and the old role's are taken away."""
    reader, q = sql.Identifier(READER_ROLE), sql.Identifier(READER_SCHEMA)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(q, reader))
    conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(q, reader))
    try:
        with conn.transaction():
            conn.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(reader))
    except psycopg.Error:
        pass  # as in 0003: only the SET ROLE fallback needs the membership
    if retire_old:
        for what in ("SCHEMA", "ALL TABLES IN SCHEMA", "ALL SEQUENCES IN SCHEMA", "ALL FUNCTIONS IN SCHEMA"):
            conn.execute(sql.SQL("REVOKE ALL ON {} {}, {} FROM {}").format(
                sql.SQL(what), sql.Identifier(SCHEMA), q, sql.Identifier(LEGACY_READER_ROLE)))


_RENAMED_SCHEMAS = {LEGACY_SCHEMA: SCHEMA, LEGACY_READER_SCHEMA: READER_SCHEMA}
_LIST_ITEM = re.compile(r'\s*(?:"((?:[^"]|"")*)"|([^,\s]+))\s*(?:,|$)')


def _setting_statements(conn: psycopg.Connection[Any], renamed: dict[str, str]) -> list[sql.Composed]:
    """ALTER ROLE / ALTER DATABASE statements that rewrite stored settings naming the old schemas: search_path
    entries and assay.* parameters, for this database (any role) and for the reader role cluster-wide. benchbook
    never stores such settings itself; an administrator may have (a BI login that reads the views, say).
    renamed: roles that are renamed by a statement before these, old name -> new name."""
    rows = conn.execute(
        "SELECT d.datname, r.rolname, unnest(s.setconfig) FROM pg_db_role_setting s"
        " LEFT JOIN pg_database d ON d.oid = s.setdatabase LEFT JOIN pg_roles r ON r.oid = s.setrole"
        " WHERE d.datname = current_database() OR (s.setdatabase = 0 AND r.rolname = ANY(%s))",
        ([READER_ROLE, *renamed],)).fetchall()
    out: list[sql.Composed] = []
    for database, role_, entry in rows:
        role = renamed.get(role_, role_) if role_ is not None else None
        name, _, value = str(entry).partition("=")
        if role is None:
            target = sql.SQL("ALTER DATABASE {}").format(sql.Identifier(database))
        elif database is None:
            target = sql.SQL("ALTER ROLE {}").format(sql.Identifier(role))
        else:
            target = sql.SQL("ALTER ROLE {} IN DATABASE {}").format(sql.Identifier(role), sql.Identifier(database))
        if name == "search_path":
            schemas = [m[1].replace('""', '"') if m[1] is not None else m[2] for m in _LIST_ITEM.finditer(value)]
            if any(s in _RENAMED_SCHEMAS for s in schemas):
                path = sql.SQL(", ").join(sql.Identifier(_RENAMED_SCHEMAS.get(s, s)) for s in schemas)
                out.append(target + sql.SQL(" SET search_path = ") + path)
        elif name.startswith(LEGACY_SCHEMA + "."):
            def parameter(n: str) -> sql.Composed:
                return sql.SQL(".").join(sql.Identifier(part) for part in n.split("."))

            out.append(target + sql.SQL(" SET ") + parameter(SCHEMA + name[len(LEGACY_SCHEMA):])
                       + sql.SQL(" = ") + sql.Literal(value))
            out.append(target + sql.SQL(" RESET ") + parameter(name))
    return out


_LEFTOVERS = """
WITH ns AS (SELECT oid, nspname, nspacl FROM pg_namespace WHERE nspname = ANY(%(schemas)s)),
rel AS (SELECT c.oid, c.relkind, c.relacl, n.nspname || '.' || c.relname AS name
        FROM pg_class c JOIN ns n ON n.oid = c.relnamespace),
fn AS (SELECT p.oid, p.prosrc, p.proacl, p.oid::regprocedure::text AS name
       FROM pg_proc p JOIN ns ON ns.oid = p.pronamespace)
SELECT 'function ' || name FROM fn WHERE prosrc ~* %(old)s
UNION ALL SELECT 'view ' || name FROM rel WHERE relkind IN ('v', 'm') AND pg_get_viewdef(oid) ~* %(old)s
UNION ALL SELECT 'default of ' || rel.name || '.' || a.attname
  FROM pg_attrdef d JOIN rel ON rel.oid = d.adrelid
  JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
  WHERE pg_get_expr(d.adbin, d.adrelid) ~* %(old)s
UNION ALL SELECT 'constraint ' || c.conname || ' on ' || coalesce(c.conrelid::regclass::text, c.contypid::regtype::text)
  FROM pg_constraint c JOIN ns ON ns.oid = c.connamespace WHERE pg_get_constraintdef(c.oid) ~* %(old)s
UNION ALL SELECT 'trigger ' || t.tgname || ' on ' || rel.name
  FROM pg_trigger t JOIN rel ON rel.oid = t.tgrelid WHERE NOT t.tgisinternal AND pg_get_triggerdef(t.oid) ~* %(old)s
UNION ALL SELECT 'index ' || i.indexrelid::regclass::text
  FROM pg_index i JOIN rel ON rel.oid = i.indrelid WHERE pg_get_indexdef(i.indexrelid) ~* %(old)s
UNION ALL SELECT 'policy ' || p.polname || ' on ' || rel.name
  FROM pg_policy p JOIN rel ON rel.oid = p.polrelid
  WHERE coalesce(pg_get_expr(p.polqual, p.polrelid), '') || ' ' ||
        coalesce(pg_get_expr(p.polwithcheck, p.polrelid), '') ~* %(old)s
UNION ALL SELECT 'privileges of ' || %(role_name)s::text || ' on ' || x.name
  FROM (SELECT nspname AS name, nspacl AS acl FROM ns UNION ALL SELECT name, relacl FROM rel
        UNION ALL SELECT name, proacl FROM fn) x
  WHERE EXISTS (SELECT 1 FROM aclexplode(x.acl) a WHERE a.grantee = %(role)s::oid)
ORDER BY 1
"""


def _leftovers(conn: psycopg.Connection[Any], retired_role: int | None) -> list[str]:
    """Definitions in the renamed schemas that still contain the old name, the retired role's remaining privileges
    there, and stored settings that still name the old schemas. Row data is not looked at: the ledger is
    append-only, so old rows keep what they say (for example assay://blob/... URIs)."""
    conn.execute("SET LOCAL search_path = pg_catalog")  # the pg_get_*def functions then print qualified names
    found = [str(r[0]) for r in conn.execute(_LEFTOVERS, {
        "schemas": [SCHEMA, READER_SCHEMA], "old": LEGACY_SCHEMA, "role": retired_role or 0,
        "role_name": LEGACY_READER_ROLE})]
    return found + [f"setting: {s.as_string(conn)}" for s in _setting_statements(conn, {})]
