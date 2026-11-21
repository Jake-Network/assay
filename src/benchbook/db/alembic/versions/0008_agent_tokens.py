"""Per-agent tokens, the benchbook_writer login and identity-bound writes.

Revision ID: 0008
Revises: 0007
"""
from benchbook.db.migrate import run_sql_file

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0008_agent_tokens.sql")


def downgrade() -> None:
    raise NotImplementedError("benchbook migrations are forward-only (append-only ledger, P1)")
