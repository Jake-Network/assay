"""Claim evidence, activity filtering and notifications for long project IDs

Revision ID: 0004
Revises: 0003
"""
from assay.db.migrate import run_sql_file

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0004_evidence_and_notices.sql")


def downgrade() -> None:
    raise NotImplementedError("assay migrations are forward-only (append-only ledger, P1)")
