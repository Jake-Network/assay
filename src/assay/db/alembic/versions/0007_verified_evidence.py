"""Guard partition settings and require verified rerun evidence to substantiate a shared result.

Revision ID: 0007
Revises: 0006
"""
from assay.db.migrate import run_sql_file

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0007_verified_evidence.sql")


def downgrade() -> None:
    raise NotImplementedError("assay migrations are forward-only (append-only ledger, P1)")
