"""benchbook_sql safety (§7.3, §18.1)."""
from __future__ import annotations

import json

import pytest

from benchbook import Ledger
from benchbook.errors import BenchbookError
from benchbook.tokens import estimate_tokens


@pytest.mark.parametrize("q", [
    "INSERT INTO v_best VALUES (1)",
    "UPDATE benchbook.experiments SET notes='x'",
    "CREATE TABLE x (a int)",
    "DROP VIEW v_best",
    "SELECT * FROM benchbook.experiments",
    "SELECT * FROM benchbook.projects",
    "SELECT 1; SELECT 2",
    "SELECT pg_sleep(5)",
    "SELECT set_config('role', 'postgres', true)",
    "WITH x AS (DELETE FROM benchbook.claims RETURNING *) SELECT * FROM x",
])
def test_rejected(lead: Ledger, q: str) -> None:
    with pytest.raises(BenchbookError):
        lead.sql(q)


def test_views_readable_and_scoped(db_url: str, lead: Ledger, worker: Ledger) -> None:
    from .conftest import make_project

    other = Ledger.open(project=make_project(db_url), agent="w", database_url=db_url)
    other.record_experiment({"other": 1}, {"score": 9})
    worker.record_experiment({"a": 1}, {"score": 1})
    r = lead.sql("SELECT primary_value, project_id FROM v_best")
    assert r["rows"] == [{"primary_value": 1.0, "project_id": lead.project_id}]
    assert lead.sql("SELECT count(*) AS n FROM v_tried")["rows"] == [{"n": 1}]


def test_truncation(lead: Ledger, worker: Ledger) -> None:
    for i in range(230):
        worker.record_experiment({"i": i}, {"score": i})
    r = lead.sql("SELECT id, primary_value FROM v_progress")
    assert r["truncated"] and r["row_count"] == 230 and r["returned"] <= 200
    r2 = lead.sql("SELECT * FROM v_tried")
    assert r2["truncated"] and r2["returned"] < 200  # token cap (4,000) bites first on wide rows
    assert estimate_tokens(json.dumps(r2, ensure_ascii=False)) <= 4000


@pytest.mark.parametrize("padding", ["a" * 20000, "가" * 5000])
def test_first_row_respects_token_limit(lead: Ledger, worker: Ledger, padding: str) -> None:
    worker.record_experiment({"padding": padding}, {"score": 1})
    response = lead.sql("SELECT config FROM v_best")
    assert response == {"columns": ["config"], "rows": [], "row_count": 1, "returned": 0, "truncated": True}


def test_empty_results_keep_column_metadata(lead: Ledger) -> None:
    result = lead.sql("SELECT primary_value AS score FROM v_best WHERE false")
    assert result == {"columns": ["score"], "rows": [], "row_count": 0, "returned": 0, "truncated": False}
