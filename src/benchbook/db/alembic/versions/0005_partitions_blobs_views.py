"""Partitions, blobs, settings audit, intents and autoresearch views (Step 3, C1-C5)

Revision ID: 0005
Revises: 0004
"""
from benchbook.db.migrate import run_sql_file

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0005_partitions_blobs_views.sql")


def downgrade() -> None:
    raise NotImplementedError("benchbook migrations are forward-only (append-only ledger, P1)")
