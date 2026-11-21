"""benchbook_reader role and benchbook_q views

Revision ID: 0003
Revises: 0002
"""
from benchbook.db.migrate import run_sql_file

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0003_roles.sql")


def downgrade() -> None:
    raise NotImplementedError("benchbook migrations are forward-only (append-only ledger, P1)")
