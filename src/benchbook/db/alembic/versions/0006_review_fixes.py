"""Review fixes: TRUNCATE and agent-role triggers, set_config revoke, seed index, v_tried and candidate views

Revision ID: 0006
Revises: 0005
"""
from benchbook.db.migrate import run_sql_file

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0006_review_fixes.sql")


def downgrade() -> None:
    raise NotImplementedError("benchbook migrations are forward-only (append-only ledger, P1)")
