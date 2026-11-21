"""Alembic environment. Migrations are raw SQL (no ORM metadata, no autogenerate)."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

from benchbook.db.migrate import LEGACY_SCHEMA, LOCK_KEY, SCHEMA, LegacySchemaConflict, _has_table

config = context.config
url = config.get_main_option("sqlalchemy.url")
connection = config.attributes.get("connection")


def _run(conn) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=conn, target_metadata=None,
                      version_table="alembic_version", version_table_schema=SCHEMA,
                      transaction_per_migration=False)
    with context.begin_transaction():
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        # migrate() renames a pre-rename ledger before it calls Alembic. If one is here all the same (an old binary
        # created it a moment ago), building a second ledger next to it would be the guess we refuse to make.
        if _has_table(conn.connection.driver_connection, LEGACY_SCHEMA):
            raise LegacySchemaConflict(f"schema {LEGACY_SCHEMA} appeared during the migration; "
                                       "run `benchbook migrate` again")
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, version_table_schema=SCHEMA)
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    _run(connection)
else:
    engine = create_engine(url, poolclass=pool.NullPool)  # type: ignore[arg-type]
    with engine.connect() as conn:
        _run(conn)
