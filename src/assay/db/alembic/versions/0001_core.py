"""core tables and triggers

Revision ID: 0001
Revises: 
"""
from assay.db.migrate import run_sql_file

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0001_core.sql")


def downgrade() -> None:
    raise NotImplementedError("assay migrations are forward-only (append-only ledger, P1)")
