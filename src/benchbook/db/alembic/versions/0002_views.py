"""read views

Revision ID: 0002
Revises: 0001
"""
from benchbook.db.migrate import run_sql_file

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0002_views.sql")


def downgrade() -> None:
    raise NotImplementedError("benchbook migrations are forward-only (append-only ledger, P1)")
