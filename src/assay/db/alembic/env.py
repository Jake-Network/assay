"""Alembic environment. Migrations are raw SQL (no ORM metadata, no autogenerate)."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

LOCK_KEY = 0x61737361  # serialises concurrent `assay migrate` from several processes

config = context.config
url = config.get_main_option("sqlalchemy.url")
connection = config.attributes.get("connection")


def _run(conn) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=conn, target_metadata=None,
                      version_table="alembic_version", version_table_schema="assay",
                      transaction_per_migration=False)
    with context.begin_transaction():
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS assay"))
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, version_table_schema="assay")
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    _run(connection)
else:
    engine = create_engine(url, poolclass=pool.NullPool)  # type: ignore[arg-type]
    with engine.connect() as conn:
        _run(conn)
