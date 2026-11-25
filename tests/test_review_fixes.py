"""Regression tests for the 2026-11 review (benchbook-review.md): C1-C7, A2, A3, I1, U2-U4, U6."""
from __future__ import annotations

import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import fmean
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from benchbook import Ledger
from benchbook.core.dedup import dedup_key
from benchbook.errors import BenchbookError, Conflict, InvalidTransition
from tests.conftest import make_project
from tests.test_core_ext import cfg, ml_project


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
    detail = lead.sql_internal("SELECT oracle_detail FROM benchbook.decisions"
                               " WHERE target_id=%s AND new_status='verified'",
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
    m = a.propose_claim("observation", "lr=2 measures 2.81", payload={"measurement": True, "mean": 2.81},
                        evidence=[(ids[0], "supports")])
    lead.decide(m.id, "supported", "metric", "first run", ids[:1])  # type: ignore[arg-type]
    assert lead.decide(m.id, "verified", "rerun", "reproduced", ids).new_status == "verified"  # type: ignore[arg-type]


def test_measurement_needs_evidence_for_claimed_mean(db_url: str) -> None:
    pid = ml_project(db_url)
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    a, b, c = (lead.as_agent(name) for name in ("a", "b", "c"))
    ids = []
    for seed, who in enumerate((a, b, c)):
        run = who.record_experiment(cfg("p"), {"val_bpb": 1.0}, seed=seed, code_ref="a").id
        assert run is not None
        ids.append(run)
    payload = {"partition": "p", "code_ref": "a", "experiment_ids": list(map(str, ids)), "origin": str(ids[0]),
               "std": 0., "n": len(ids), "measurement": True}
    wrong = a.propose_claim("result", "Measured 0.5", payload={**payload, "mean": 0.5},
                            evidence=[(ids[0], "supports")]).id
    lead.decide(wrong, "supported", "metric", "first run", ids[:1])
    with pytest.raises(InvalidTransition, match="payload.mean must match"):
        lead.decide(wrong, "verified", "rerun", "reproduced", ids)
    # Older clients allowed this decision. A historical row stays in the ledger but cannot supply the baseline.
    with psycopg.connect(db_url) as conn:
        conn.execute("INSERT INTO benchbook.decisions(project_id,target_type,target_id,new_status,oracle,rationale,"
                     "evidence_ids,decided_by) VALUES (%s,'claim',%s,'verified','rerun','legacy result',%s,'lead')",
                     (pid, wrong, ids))
    assert lead.claim_status(wrong) == "verified"
    assert lead.baseline("p")[0] == 1.0
    for field, value in (("partition", "elsewhere"), ("code_ref", "other"), ("origin", str(ids[1]))):
        mismatch = a.propose_claim("result", f"Wrong {field}", payload={**payload, "mean": 1.0, field: value},
                                   evidence=[(ids[0], "supports")]).id
        lead.decide(mismatch, "supported", "metric", "first run", ids[:1])
        with pytest.raises(InvalidTransition, match="must match the original experiment"):
            lead.decide(mismatch, "verified", "rerun", "reproduced", ids)
    del payload["origin"]
    claim = a.propose_claim("result", "Measured 1.0", payload={**payload, "mean": 1.0},
                            evidence=[(ids[0], "supports")]).id
    lead.decide(claim, "supported", "metric", "first run", ids[:1])
    assert lead.decide(claim, "verified", "rerun", "reproduced", ids).new_status == "verified"
    best = lead.sql_internal("SELECT ref_id, origin_id FROM benchbook.v_partition_best"
                             " WHERE project_id=%s AND status='verified'", (pid,))
    assert best == [{"ref_id": claim, "origin_id": ids[0]}]


def test_self_rerun_does_not_mask_a_failing_independent_rerun(db_url: str) -> None:
    pid = ml_project(db_url)
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    a, b = lead.as_agent("a"), lead.as_agent("b")
    a.record_experiment(cfg("p", "base"), {"val_bpb": 2.91}, code_ref="base")
    ids = []
    for seed, (who, score) in enumerate(((a, 2.80), (a, 2.80), (b, 2.96))):
        run = who.record_experiment(cfg("p"), {"val_bpb": score}, seed=seed, code_ref="a").id
        assert run is not None
        ids.append(run)
    mean = fmean((2.80, 2.80, 2.96))
    claim = a.propose_claim("result", "trial code beats baseline",
                            payload={"partition": "p", "code_ref": "a", "experiment_ids": list(map(str, ids)),
                                     "origin": str(ids[0]), "mean": mean, "std": None, "n": len(ids)},
                            evidence=[(ids[0], "supports")])
    lead.decide(claim.id, "supported", "metric", "first run", ids[:1])
    with pytest.raises(InvalidTransition, match="independent reruns 2.96"):
        lead.decide(claim.id, "verified", "rerun", "three runs", ids)
    archived = {"rerun_check": {"mean": mean, "rerun_mean": fmean((2.8, 2.96)), "original": str(ids[0]),
                                "baseline": 2.91, "epsilon": 0.0005}}
    with psycopg.connect(db_url) as conn:
        conn.execute("INSERT INTO benchbook.decisions(project_id,target_type,target_id,new_status,oracle,rationale,"
                     "evidence_ids,decided_by,oracle_detail)"
                     " VALUES (%s,'claim',%s,'verified','rerun','legacy result',%s,'lead',%s)",
                     (pid, claim.id, ids, Jsonb(archived)))
    assert lead.sql_internal("SELECT ref_id FROM benchbook.v_partition_best"
                             " WHERE project_id=%s AND status='verified'", (pid,)) == []


# C2 ------------------------------------------------------------------------------------------------
def test_c2_truncate_and_agent_role_changes_are_refused(db_url: str, lead: Ledger, worker: Ledger) -> None:
    worker.record_experiment({"a": 1}, {"score": 1})
    with psycopg.connect(db_url) as conn:
        for stmt in ("TRUNCATE benchbook.experiments CASCADE", "TRUNCATE benchbook.dedup_hits",
                     "UPDATE benchbook.agents SET role='orchestrator' WHERE id='worker-1'",
                     "DELETE FROM benchbook.agents WHERE id='worker-1'"):
            with pytest.raises(psycopg.errors.RestrictViolation):
                with conn.transaction():
                    conn.execute(stmt)  # type: ignore[arg-type]
        # metadata that is not the role may still change
        with conn.transaction():
            conn.execute("UPDATE benchbook.agents SET model='m' WHERE project_id=%s AND id='worker-1'",
                         (lead.project_id,))


# C4 ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("query", [
    "SELECT U&\"set\\005fconfig\"('benchbook.project','other',true),"
    " query_to_xml('select * from v_tried',true,false,'')",
    "SELECT u&'x'",
    "SELECT query_to_xml('select 1', true, false, '')",
    "SELECT * FROM pg_settings",
])
def test_c4_sql_rejects_escape_hatches(lead: Ledger, query: str) -> None:
    with pytest.raises(BenchbookError, match="forbidden"):
        lead.sql(query)


def test_c4_reader_cannot_call_set_config(db_url: str) -> None:
    with psycopg.connect(db_url, user="benchbook_reader") as conn:
        is_super = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()
        assert is_super is not None and not is_super[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT set_config('benchbook.project', 'other', true)")


def test_c4_sql_still_reads_its_project(lead: Ledger, worker: Ledger) -> None:
    worker.record_experiment({"a": 1}, {"score": 1})
    assert lead.sql("SELECT count(*) AS n FROM v_tried")["rows"] == [{"n": 1}]


# C5 ------------------------------------------------------------------------------------------------
def test_c5_lucky_seed_does_not_label_config_improved(db_url: str) -> None:
    lead, a, b, c = lab(db_url)
    a.record_experiment({"lr": 1}, {"val_loss": 2.91})
    assert a.record_experiment({"lr": 2}, {"val_loss": 2.80}, seed=0).outcome == "improved"
    rows = lead.sql_internal("SELECT summary_outcome, seeds FROM benchbook.v_tried WHERE project_id=%s"
                             " AND config->>'lr' = '2'", (lead.project_id,))
    assert rows[0]["summary_outcome"] == "improved"
    b.record_experiment({"lr": 2}, {"val_loss": 2.99}, seed=1)
    c.record_experiment({"lr": 2}, {"val_loss": 3.01}, seed=2)  # mean 2.93, worse than 2.91
    rows = lead.sql_internal("SELECT summary_outcome FROM benchbook.v_tried WHERE project_id=%s"
                             " AND config->>'lr' = '2'", (lead.project_id,))
    assert rows[0]["summary_outcome"] == "worse"


def test_v_tried_uses_project_epsilon_for_legacy_settings(db_url: str) -> None:
    lead, a, b, _ = lab(db_url, epsilon=0.1,
                        settings={"partition_key": "partition", "partitions": {"p": {"epsilon": False}}})
    a.record_experiment({"partition": "p", "lr": 1}, {"val_loss": 1.0})
    a.record_experiment({"partition": "p", "lr": 2}, {"val_loss": 0.95}, seed=0)
    b.record_experiment({"partition": "p", "lr": 2}, {"val_loss": 0.95}, seed=1)
    assert lead.sql("SELECT summary_outcome FROM v_tried WHERE config->>'lr' = '2'")["rows"] == [
        {"summary_outcome": "no_change"}]


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
    rows = lead.sql_internal("SELECT ref_id FROM benchbook.v_partition_best WHERE project_id=%s AND status='candidate'"
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


def test_parallel_claims_cannot_reserve_two_seeds_for_one_agent(db_url: str, lead: Ledger) -> None:
    spec = {"origin": "same", "distinct_agents_for": "origin"}
    ids = lead.create_tasks([{"title": f"rep {seed}", "spec": {**spec, "seed": seed}} for seed in (1, 2)])
    workers = [lead.as_agent("replicator") for _ in ids]
    lock_key = ("benchbook:task-claim:" + lead.project_id, "replicator")
    with ThreadPoolExecutor(max_workers=2) as pool:
        with psycopg.connect(db_url) as blocker, psycopg.connect(db_url, autocommit=True) as observer:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", lock_key)
            claims = [pool.submit(w.claim_task, tid) for w, tid in zip(workers, ids, strict=True)]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                waiting = observer.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted AND objsubid=2"
                    " AND classid=(hashtext(%s)::bigint & 4294967295)::oid"
                    " AND objid=(hashtext(%s)::bigint & 4294967295)::oid", lock_key,
                ).fetchone()
                if waiting == (2,):
                    break
                if all(claim.done() for claim in claims):
                    pytest.fail("parallel claims did not wait for the per-agent lock")
                time.sleep(0.01)
            else:
                pytest.fail("parallel claims did not both reach the per-agent lock")
        successes = []
        for result in claims:
            try:
                successes.append(result.result(timeout=10))
            except Conflict:
                continue
        assert len(successes) == 1
    other = lead.as_agent("other")
    assert other.next_task() is not None


# I1 ------------------------------------------------------------------------------------------------
def test_i1_no_silent_human(db_url: str, project: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCHBOOK_AGENT", raising=False)
    with pytest.raises(BenchbookError, match="BENCHBOOK_AGENT"):
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
    left = lead.sql_internal("SELECT extract(epoch FROM lease_expires_at - now()) AS s"
                             " FROM benchbook.tasks WHERE id=%s",
                             (b,))[0]["s"]
    assert float(left) > 7000
    with pytest.raises(Conflict, match="not claimable"):
        lead.as_agent("w2").claim_task(b)
    lead.update_settings({"lease_seconds": 300})
    w.heartbeat(b)
    left = lead.sql_internal("SELECT extract(epoch FROM lease_expires_at - now()) AS s"
                             " FROM benchbook.tasks WHERE id=%s",
                             (b,))[0]["s"]
    assert 299 < float(left) <= 300


# U6 ------------------------------------------------------------------------------------------------
def test_u6_worktree_finds_main_config(tmp_path: Path) -> None:
    from benchbook.config import find_config

    main = tmp_path / "main"
    main.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=main, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    (main / ".benchbook").mkdir()
    (main / ".benchbook" / "config.toml").write_text('project = "p"\n')
    wt = tmp_path / f"wt-{uuid.uuid4().hex[:6]}"
    run("worktree", "add", "-q", str(wt))
    found = find_config(wt)
    assert found is not None and found.resolve() == (main / ".benchbook" / "config.toml").resolve()
