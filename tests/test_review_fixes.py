"""Regression tests for the 2026-11 review (assay-review.md): C1-C7, A2, A3, I1, U2-U4, U6."""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from assay import Ledger
from assay.dedup import dedup_key
from assay.errors import AssayError, Conflict, InvalidTransition
from tests.conftest import make_project


def lab(db_url: str, **kw: Any) -> tuple[Ledger, Ledger, Ledger, Ledger]:
    pid = make_project(db_url, metric="val_loss", direction="min", **kw)
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    return lead, lead.as_agent("a"), lead.as_agent("b"), lead.as_agent("c")


# C1 ------------------------------------------------------------------------------------------------
def test_c1_rerun_that_does_not_reproduce_is_not_verified(db_url: str) -> None:
    lead, a, b, c = lab(db_url)
    a.record_experiment({"lr": 1}, {"val_loss": 2.91})  # baseline
    orig = a.record_experiment({"lr": 2}, {"val_loss": 2.80}, seed=0).id  # lucky seed
    reruns = [b.record_experiment({"lr": 2}, {"val_loss": 2.95}, seed=1).id,
              c.record_experiment({"lr": 2}, {"val_loss": 2.97}, seed=2).id]
    claim = a.propose_claim("observation", "lr=2 beats lr=1", evidence=[(orig, "supports")])
    lead.decide(claim.id, "supported", "metric", "first run", [orig])  # type: ignore[list-item]
    with pytest.raises(InvalidTransition, match="do not reproduce"):
        lead.decide(claim.id, "verified", "rerun", "three runs", [orig, *reruns])  # type: ignore[list-item]


def test_c1_rerun_that_reproduces_records_the_check(db_url: str) -> None:
    lead, a, b, c = lab(db_url)
    a.record_experiment({"lr": 1}, {"val_loss": 2.91})
    ids = [a.record_experiment({"lr": 2}, {"val_loss": 2.80}, seed=0).id,
           b.record_experiment({"lr": 2}, {"val_loss": 2.82}, seed=1).id,
           c.record_experiment({"lr": 2}, {"val_loss": 2.81}, seed=2).id]
    claim = a.propose_claim("observation", "lr=2 beats lr=1 (good)", evidence=[(ids[0], "supports")])
    lead.decide(claim.id, "supported", "metric", "first run", ids[:1])  # type: ignore[arg-type]
    lead.decide(claim.id, "verified", "rerun", "reproduced", ids)  # type: ignore[arg-type]
    detail = lead.sql_internal("SELECT oracle_detail FROM assay.decisions WHERE target_id=%s AND new_status='verified'",
                               (claim.id,))[0]["oracle_detail"]["rerun_check"]
    assert detail["baseline"] == pytest.approx(2.91) and detail["mean"] == pytest.approx(2.81)
    assert detail["rerun_mean"] == pytest.approx(2.815)


def test_c1_without_baseline_needs_measurement_flag(db_url: str) -> None:
    lead, a, b, c = lab(db_url)
    ids = [a.record_experiment({"lr": 2}, {"val_loss": 2.80}, seed=0).id,
           b.record_experiment({"lr": 2}, {"val_loss": 2.82}, seed=1).id,
           c.record_experiment({"lr": 2}, {"val_loss": 2.81}, seed=2).id]
    claim = a.propose_claim("observation", "lr=2 gets 2.81", evidence=[(ids[0], "supports")])
    lead.decide(claim.id, "supported", "metric", "first run", ids[:1])  # type: ignore[arg-type]
    with pytest.raises(InvalidTransition, match="no numeric payload.baseline"):
        lead.decide(claim.id, "verified", "rerun", "reproduced", ids)  # type: ignore[arg-type]
    m = a.propose_claim("observation", "lr=2 measures 2.81", payload={"measurement": True},
                        evidence=[(ids[0], "supports")])
    lead.decide(m.id, "supported", "metric", "first run", ids[:1])  # type: ignore[arg-type]
    assert lead.decide(m.id, "verified", "rerun", "reproduced", ids).new_status == "verified"  # type: ignore[arg-type]


# C2 ------------------------------------------------------------------------------------------------
def test_c2_truncate_and_agent_role_changes_are_refused(db_url: str, lead: Ledger, worker: Ledger) -> None:
    worker.record_experiment({"a": 1}, {"score": 1})
    with psycopg.connect(db_url) as conn:
        for stmt in ("TRUNCATE assay.experiments CASCADE", "TRUNCATE assay.dedup_hits",
                     "UPDATE assay.agents SET role='orchestrator' WHERE id='worker-1'",
                     "DELETE FROM assay.agents WHERE id='worker-1'"):
            with pytest.raises(psycopg.errors.RestrictViolation):
                with conn.transaction():
                    conn.execute(stmt)  # type: ignore[arg-type]
        # metadata that is not the role may still change
        with conn.transaction():
            conn.execute("UPDATE assay.agents SET model='m' WHERE project_id=%s AND id='worker-1'", (lead.project_id,))


# C4 ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("query", [
    "SELECT U&\"set\\005fconfig\"('assay.project','other',true), query_to_xml('select * from v_tried',true,false,'')",
    "SELECT u&'x'",
    "SELECT query_to_xml('select 1', true, false, '')",
    "SELECT * FROM pg_settings",
])
def test_c4_sql_rejects_escape_hatches(lead: Ledger, query: str) -> None:
    with pytest.raises(AssayError, match="forbidden"):
        lead.sql(query)


def test_c4_reader_cannot_call_set_config(db_url: str) -> None:
    with psycopg.connect(db_url, user="assay_reader") as conn:
        is_super = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        assert is_super is not None and not is_super[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT set_config('assay.project', 'other', true)")


def test_c4_sql_still_reads_its_project(lead: Ledger, worker: Ledger) -> None:
    worker.record_experiment({"a": 1}, {"score": 1})
    assert lead.sql("SELECT count(*) AS n FROM v_tried")["rows"] == [{"n": 1}]


# C5 ------------------------------------------------------------------------------------------------
def test_c5_lucky_seed_does_not_label_config_improved(db_url: str) -> None:
    lead, a, b, c = lab(db_url)
    a.record_experiment({"lr": 1}, {"val_loss": 2.91})
    assert a.record_experiment({"lr": 2}, {"val_loss": 2.80}, seed=0).outcome == "improved"
    rows = lead.sql_internal("SELECT summary_outcome, seeds FROM assay.v_tried WHERE project_id=%s"
                             " AND config->>'lr' = '2'", (lead.project_id,))
    assert rows[0]["summary_outcome"] == "improved"
    b.record_experiment({"lr": 2}, {"val_loss": 2.99}, seed=1)
    c.record_experiment({"lr": 2}, {"val_loss": 3.01}, seed=2)  # mean 2.93, worse than 2.91
    rows = lead.sql_internal("SELECT summary_outcome FROM assay.v_tried WHERE project_id=%s"
                             " AND config->>'lr' = '2'", (lead.project_id,))
    assert rows[0]["summary_outcome"] == "worse"


# C6 ------------------------------------------------------------------------------------------------
def test_c6_check_tried_mentions_invalidated_runs(lead: Ledger, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 1}, {"score": 1}, seed=0)
    lead.decide(e.id, "invalidated", "human", "evaluator bug")  # type: ignore[arg-type]
    t = worker.check_tried({"a": 1})
    assert t.exact == [] and len(t.invalidated) == 1 and t.note and "code_ref" in t.note
    assert worker.record_experiment({"a": 1}, {"score": 1}, seed=0).duplicate_of == e.id


# C7 ------------------------------------------------------------------------------------------------
def test_c7_seed_none_and_minus_one_differ(worker: Ledger) -> None:
    assert worker.record_experiment({"a": 1}, {"score": 1}).id is not None
    assert worker.record_experiment({"a": 1}, {"score": 1}, seed=-1).id is not None
    assert worker.record_experiment({"a": 1}, {"score": 1}).duplicate_of is not None


def test_c7_nested_ignore_keys() -> None:
    a = {"optimizer": {"lr": 1, "log_every": 10}, "x": 1}
    b = {"optimizer": {"lr": 1, "log_every": 99}, "x": 1}
    assert dedup_key(a, None, ["optimizer.log_every"]) == dedup_key(b, None, ["optimizer.log_every"])
    assert dedup_key(a, None, []) != dedup_key(b, None, [])
    assert dedup_key(a, None, ["optimizer.lr"]) != dedup_key(b, None, ["optimizer.lr"])


def test_c7_candidate_whatever_the_first_seed(db_url: str) -> None:
    lead, a, _, _ = lab(db_url, settings={"partition_key": "partition"})
    a.record_experiment({"partition": "p", "lr": 1}, {"val_loss": 3.0}, seed=42)
    e = a.record_experiment({"partition": "p", "lr": 2}, {"val_loss": 2.0}, seed=42)
    rows = lead.sql_internal("SELECT ref_id FROM assay.v_partition_best WHERE project_id=%s AND status='candidate'"
                             " AND rank=1", (lead.project_id,))
    assert rows and rows[0]["ref_id"] == e.id


# A3 ------------------------------------------------------------------------------------------------
def test_a3_one_agent_replicates_an_origin_once(lead: Ledger) -> None:
    spec = {"origin": "o1", "exclude_agents": ["a"], "distinct_agents_for": "origin"}
    lead.create_tasks([{"title": f"rep {s}", "spec": {**spec, "seed": s}} for s in (1, 2)])
    b, c = lead.as_agent("b"), lead.as_agent("c")
    assert lead.as_agent("a").next_task() is None
    t = b.next_task()
    assert t is not None
    b.complete_task(t.id, "done")
    assert b.next_task() is None  # b already re-ran o1
    assert c.next_task() is not None


# I1 ------------------------------------------------------------------------------------------------
def test_i1_no_silent_human(db_url: str, project: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASSAY_AGENT", raising=False)
    with pytest.raises(AssayError, match="ASSAY_AGENT"):
        Ledger.open(project=project, database_url=db_url)


# U2, U3, U4 ----------------------------------------------------------------------------------------
def test_u2_brief_shows_related_findings(lead: Ledger) -> None:
    lead.create_tasks([{"title": "piece a", "tags": ["mod:publish", "piece:a"]},
                       {"title": "piece c", "tags": ["mod:publish", "piece:c"]}])
    w1, w2 = lead.as_agent("w1"), lead.as_agent("w2")
    ta = w1.next_task(tags=["piece:a"])
    assert ta is not None
    w1.propose_claim("observation", "rules.py X double counts Y", tags=["mod:publish"])
    tc = w2.next_task(tags=["piece:c"])
    assert tc is not None
    b = w2.brief(task_id=tc.id)
    assert "Related findings" in b and "rules.py X double counts Y" in b


def test_u3_experiment_links_to_the_only_lease(lead: Ledger, worker: Ledger) -> None:
    [tid] = lead.create_tasks([{"title": "t", "tags": ["x"]}])
    assert worker.next_task() is not None
    e = worker.record_experiment({"approach": "A"}, {}, run_status="error", notes="dead end")
    assert worker.get(e.id)["record"]["task_id"] == str(tid)  # type: ignore[arg-type]


def test_u4_claim_by_id_and_lease_setting(db_url: str) -> None:
    pid = make_project(db_url, settings={"lease_seconds": 7200})
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    a, b = lead.create_tasks([{"title": "a", "priority": 9}, {"title": "b"}])
    w = lead.as_agent("w")
    t = w.claim_task(b)
    assert t.id == b and t.lease_expires_at is not None
    left = lead.sql_internal("SELECT extract(epoch FROM lease_expires_at - now()) AS s FROM assay.tasks WHERE id=%s",
                             (b,))[0]["s"]
    assert float(left) > 7000
    with pytest.raises(Conflict, match="not claimable"):
        lead.as_agent("w2").claim_task(b)


# U6 ------------------------------------------------------------------------------------------------
def test_u6_worktree_finds_main_config(tmp_path: Path) -> None:
    from assay.config import find_config

    main = tmp_path / "main"
    main.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=main, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    (main / ".assay").mkdir()
    (main / ".assay" / "config.toml").write_text('project = "p"\n')
    wt = tmp_path / f"wt-{uuid.uuid4().hex[:6]}"
    run("worktree", "add", "-q", str(wt))
    found = find_config(wt)
    assert found is not None and found.resolve() == (main / ".assay" / "config.toml").resolve()
