"""Let a notify-mode pack record a repeat of an ok run: experiments.duplicate_of and dedup_hits kind 'notified'.

Revision ID: 0009
Revises: 0008
"""
from benchbook.db.migrate import run_sql_file

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0009_dedup_notify.sql")


def downgrade() -> None:
    raise NotImplementedError("benchbook migrations are forward-only (append-only ledger, P1)")
