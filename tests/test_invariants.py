"""§18.1 invariant tests I1-I7."""
from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg import sql

from benchbook import Ledger
from benchbook.db.conn import notification_channel
from benchbook.errors import InvalidTransition, LeaseLost, PermissionDenied, ValidationFailed

from .conftest import make_project

KNOWLEDGE = ["experiments", "claims", "sources", "links", "decisions", "task_events", "dedup_hits", "brief_log"]


def _seed_rows(lead: Ledger, worker: Ledger) -> None:
    t = lead.create_tasks([{"title": "t"}])[0]
    task = worker.next_task()
    assert task is not None and task.id == t
    e = worker.record_experiment({"a": 1}, {"score": 1.0}, seed=0, task_id=t)
    c = worker.propose_claim("hypothesis", "a=1 is good", evidence=[(e.id, "supports")])  # type: ignore[list-item]
    worker.add_source("https://example.org")
    lead.decide(c.id, "supported", oracle="metric", rationale="r", evidence_ids=[e.id])  # type: ignore[list-item]
    worker.check_tried({"a": 1})
    worker.brief()


# I1 ---------------------------------------------------------------------------
@pytest.mark.parametrize("table", KNOWLEDGE)
def test_i1_append_only(db_url: str, lead: Ledger, worker: Ledger, table: str) -> None:
    _seed_rows(lead, worker)
    with psycopg.connect(db_url) as conn:  # superuser/owner role
        for stmt in (f"UPDATE benchbook.{table} SET project_id = project_id WHERE project_id = %s",
                     f"DELETE FROM benchbook.{table} WHERE project_id = %s"):
            with pytest.raises(psycopg.errors.RestrictViolation):
                with conn.transaction():
                    conn.execute(stmt, (lead.project_id,))


def test_i1_reader_role_cannot_write(db_url: str, lead: Ledger, worker: Ledger) -> None:
    _seed_rows(lead, worker)
    with psycopg.connect(db_url, user="benchbook_reader") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE benchbook.experiments SET notes = 'x'")


# I2 ---------------------------------------------------------------------------
def test_i2_concurrent_claims(db_url: str) -> None:
    pid = make_project(db_url)
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    n_tasks, n_workers = 500, 32
    lead.create_tasks([{"title": f"task {i}"} for i in range(n_tasks)])
    done: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(i: int) -> None:
        try:
            w = Ledger.open(project=pid, agent=f"w{i}", database_url=db_url)
            while (t := w.next_task(lease_seconds=60)) is not None:
                w.complete_task(t.id, "done", "ok")
                with lock:
                    done.append(str(t.id))
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n_workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert len(done) == n_tasks and len(set(done)) == n_tasks
    rows = lead.sql_internal(
        "SELECT task_id, count(*) AS n FROM benchbook.task_events WHERE project_id=%s AND event='claimed'"
        " GROUP BY task_id", (pid,))
    assert all(r["n"] == 1 for r in rows) and len(rows) == n_tasks


def test_i2_lease_expiry_and_exhaustion(db_url: str) -> None:
    pid = make_project(db_url)
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    a = Ledger.open(project=pid, agent="a", database_url=db_url)
    b = Ledger.open(project=pid, agent="b", database_url=db_url)
    lead.create_tasks([{"title": "x", "max_attempts": 2}])
    t1 = a.next_task(lease_seconds=1)
    assert t1 is not None and b.next_task() is None
    time.sleep(1.2)
    t2 = b.next_task(lease_seconds=1)
    assert t2 is not None and t2.id == t1.id and t2.attempts == 2 and t2.lease_owner == "b"
    with pytest.raises(LeaseLost):
        a.heartbeat(t1.id)
    with pytest.raises(LeaseLost):
        a.complete_task(t1.id, "done", "")
    time.sleep(1.2)
    assert a.next_task() is None  # exhausted: becomes failed
    assert lead.get_task(t1.id).status == "failed"
    events = [e["event"] for e in lead.get(t1.id)["events"]]
    assert events == ["created", "claimed", "expired", "claimed", "expired", "failed"]


def test_dependencies_respected(lead: Ledger, worker: Ledger) -> None:
    [a] = lead.create_tasks([{"title": "first"}])
    [b] = lead.create_tasks([{"title": "second", "depends_on": [str(a)], "priority": 10}])
    t = worker.next_task()
    assert t is not None and t.id == a
    assert worker.next_task() is None
    worker.complete_task(a, "done", "")
    t2 = worker.next_task()
    assert t2 is not None and t2.id == b


# I3 ---------------------------------------------------------------------------
def test_i3_verified_needs_oracle_and_evidence(db_url: str, lead: Ledger, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 1}, {"score": 1.0})
    c = worker.propose_claim("observation", "a works")
    lead.decide(c.id, "supported", "metric", "ok", [e.id])  # type: ignore[list-item]
    with pytest.raises(InvalidTransition):
        lead.decide(c.id, "verified", "llm_judge", "judge says so", [e.id])  # type: ignore[list-item]
    with pytest.raises(InvalidTransition):
        lead.decide(c.id, "verified", "metric", "no evidence", [])
    # DB CHECK is the backstop
    with psycopg.connect(db_url) as conn:
        for oracle, ev in (("llm_judge", [e.id]), ("metric", [])):
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO benchbook.decisions(project_id,target_type,target_id,new_status,oracle,rationale,"
                        "evidence_ids,decided_by) VALUES (%s,'claim',%s,'verified',%s,'x',%s,'lead')",
                        (lead.project_id, c.id, oracle, ev))
    d = lead.decide(c.id, "verified", "metric", "measured", [e.id])  # type: ignore[list-item]
    assert d.new_status == "verified" and lead.claim_status(c.id) == "verified"


def test_rerun_rule(lead: Ledger) -> None:
    w1 = lead.as_agent("w1", "worker")
    w2 = lead.as_agent("w2", "worker")
    w1.record_experiment({"a": 0}, {"score": 0.5})  # the baseline the claim improves on
    ids = [w1.record_experiment({"a": 1}, {"score": 1.0}, seed=s).id for s in (0, 1)]
    c = w1.propose_claim("observation", "a=1 scores 1", evidence=[(i, "supports") for i in ids])  # type: ignore[misc]
    lead.decide(c.id, "supported", "metric", "two seeds", ids)  # type: ignore[arg-type]
    with pytest.raises(InvalidTransition, match="seeds"):
        lead.decide(c.id, "verified", "rerun", "rerun", ids)  # type: ignore[arg-type]
    ids.append(w2.record_experiment({"a": 1}, {"score": 1.0}, seed=2).id)
    lead.decide(c.id, "verified", "rerun", "3 seeds, 2 agents", ids)  # type: ignore[arg-type]


def test_rerun_evidence_matches_original_config_and_code(lead: Ledger, worker: Ledger) -> None:
    other = lead.as_agent("worker-2")
    worker.record_experiment({"a": 0}, {"score": 0}, code_ref="v1")  # baseline
    original = worker.record_experiment({"a": 1}, {"score": 1}, seed=0, code_ref="v1").id
    same = other.record_experiment({"a": 1}, {"score": 1}, seed=1, code_ref="v1").id
    wrong_code = other.record_experiment({"a": 1}, {"score": 1}, seed=2, code_ref="v2").id
    wrong_config = [other.record_experiment({"a": 2}, {"score": 1}, seed=seed, code_ref="v1").id
                    for seed in (0, 1)]
    wrong_config += [worker.record_experiment({"a": 2}, {"score": 1}, seed=2, code_ref="v1").id]
    assert original and same and wrong_code and all(wrong_config)
    c = worker.propose_claim("observation", "a=1 scores 1")
    lead.decide(c.id, "supported", "metric", "original supporting run", [original])
    with pytest.raises(InvalidTransition, match="same config and code reference"):
        lead.decide(c.id, "verified", "rerun", "different code", [original, same, wrong_code], force=True)
    with pytest.raises(InvalidTransition, match="original supporting experiment"):
        lead.decide(c.id, "verified", "rerun", "different experiment", wrong_config)  # type: ignore[arg-type]
    last = worker.record_experiment({"a": 1}, {"score": 1}, seed=2, code_ref="v1").id
    assert last is not None
    assert lead.decide(c.id, "verified", "rerun", "proper replicates", [original, same, last]).new_status == "verified"


def test_rerun_checks_original_author_with_custom_threshold(db_url: str) -> None:
    pid = make_project(db_url, settings={"rerun_min_seeds": 1, "rerun_min_authors": 1})
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    worker = lead.as_agent("worker")
    worker.record_experiment({"a": 0}, {"score": 0})  # baseline
    original = worker.record_experiment({"a": 1}, {"score": 1}, seed=0).id
    assert original is not None
    c = worker.propose_claim("observation", "a=1 scores 1", evidence=[(original, "supports")])
    lead.decide(c.id, "supported", "metric", "there is an original run")
    with pytest.raises(InvalidTransition, match="someone other than worker"):
        lead.decide(c.id, "verified", "rerun", "self replication", [original])
    independent = lead.as_agent("other").record_experiment({"a": 1}, {"score": 1}, seed=1).id
    assert independent is not None
    assert lead.decide(c.id, "verified", "rerun", "independent", [independent]).new_status == "verified"


def test_state_graph(lead: Ledger, worker: Ledger) -> None:
    c = worker.propose_claim("hypothesis", "h")
    lead.decide(c.id, "refuted", "metric", "no", [])
    with pytest.raises(InvalidTransition):
        lead.decide(c.id, "supported", "metric", "yes")
    lead.decide(c.id, "supported", "human", "changed my mind", force=True)
    assert lead.claim_status(c.id) == "supported"


def test_competing_decisions_recheck_current_claim_state(db_url: str, lead: Ledger, worker: Ledger) -> None:
    c = worker.propose_claim("hypothesis", "h")
    evidence = worker.record_experiment({"a": 1}, {"score": 1}).id
    assert evidence is not None
    lead.decide(c.id, "supported", "metric", "initial support", [evidence])
    other = lead.as_agent("other-lead", "orchestrator")

    with ThreadPoolExecutor(max_workers=2) as pool:
        with psycopg.connect(db_url) as blocker, psycopg.connect(db_url, autocommit=True) as observer:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lead.project_id,))
            pending = [pool.submit(writer.decide, c.id, status, "human", "concurrent decision", [evidence])
                       for writer, status in ((lead, "verified"), (other, "refuted"))]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                row = observer.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"
                    " AND classid=((hashtext(%s)::bigint >> 32) & 4294967295)::oid"
                    " AND objid=(hashtext(%s)::bigint & 4294967295)::oid",
                    (lead.project_id, lead.project_id)).fetchone()
                if row == (2,):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("both decisions should wait for the project writer lock")

        accepted = 0
        for result in pending:
            try:
                result.result(timeout=10)
            except InvalidTransition:
                continue
            accepted += 1
        assert accepted == 1
    assert len(lead.get(c.id)["decisions"]) == 2


# I4 ---------------------------------------------------------------------------
def test_i4_exact_duplicate_rejected(lead: Ledger, worker: Ledger) -> None:
    r1 = worker.record_experiment({"init": "hex", "lr": 0.1}, {"score": 1.0}, seed=0, code_ref="abc")
    r2 = worker.record_experiment({"lr": 0.1, "init": "hex", "notes": "ignored"}, {"score": 1.1}, seed=0,
                                  code_ref="abc")
    assert r1.id and r2.id is None and r2.duplicate_of == r1.id
    r3 = worker.record_experiment({"init": "hex", "lr": 0.1}, {"score": 1.2}, seed=1, code_ref="abc")
    assert r3.id is not None
    hits = lead.sql_internal("SELECT kind FROM benchbook.dedup_hits WHERE project_id=%s", (lead.project_id,))
    assert [h["kind"] for h in hits] == ["rejected"]
    tried = lead.sql_internal("SELECT seeds, attempts FROM benchbook.v_tried WHERE project_id=%s", (lead.project_id,))
    assert tried == [{"seeds": 2, "attempts": 2}]
    # failed runs may be retried with the same seed
    f1 = worker.record_experiment({"x": 1}, {}, run_status="error", seed=0, notes="boom")
    f2 = worker.record_experiment({"x": 1}, {}, run_status="error", seed=0, notes="boom")
    assert f1.id and f2.id and f1.outcome == "failed"


def test_i4_concurrent_duplicates(db_url: str, project: str) -> None:
    results = []

    def go(i: int) -> None:
        w = Ledger.open(project=project, agent=f"w{i}", database_url=db_url)
        results.append(w.record_experiment({"same": True}, {"score": 1.0}, seed=7))

    ths = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert sum(r.id is not None for r in results) == 1
    assert sum(r.duplicate_of is not None for r in results) == 7


def test_check_tried_records_avoided(worker: Ledger, lead: Ledger) -> None:
    assert worker.check_tried({"a": 1}).exact == []
    worker.record_experiment({"a": 1}, {"score": 2.0}, seed=0)
    t = worker.check_tried({"a": 1.0000000000001})
    assert len(t.exact) == 1 and t.replicates == 1 and t.best_value == 2.0
    assert lead.status()["dedup"] == {"rejected": 0, "avoided": 1}


# I6 ---------------------------------------------------------------------------
def test_i6_worker_cannot_decide(db_url: str, worker: Ledger) -> None:
    c = worker.propose_claim("note", "n")
    with pytest.raises(PermissionDenied):
        worker.decide(c.id, "supported", "metric", "self-approval")
    with pytest.raises(PermissionDenied):
        worker.create_tasks([{"title": "x"}])
    with psycopg.connect(db_url) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO benchbook.decisions(project_id,target_type,target_id,new_status,oracle,rationale,"
                "decided_by)"
                " VALUES (%s,'claim',%s,'supported','metric','x','worker-1')", (worker.project_id, c.id))


# I7 ---------------------------------------------------------------------------
def test_i7_invalidated_excluded(lead: Ledger, worker: Ledger) -> None:
    a = worker.record_experiment({"v": 1}, {"score": 1.0})
    b = worker.record_experiment({"v": 2}, {"score": 5.0})  # exploits an evaluator bug
    assert b.is_new_best
    c = worker.propose_claim("observation", "v=2 is great", evidence=[(b.id, "supports")])  # type: ignore[list-item]
    with pytest.raises(InvalidTransition):
        lead.decide(b.id, "refuted", "human", "x")  # type: ignore[arg-type]
    lead.decide(b.id, "invalidated", "human", "evaluator bug")  # type: ignore[arg-type]
    st = lead.status()
    assert st["best"]["id"] == f"e:{str(a.id)[:8]}"
    r = worker.record_experiment({"v": 3}, {"score": 2.0})
    assert r.outcome == "improved"  # judged against 1.0, not the invalidated 5.0
    keys = lead.sql_internal("SELECT dedup_key FROM benchbook.v_tried WHERE project_id=%s", (lead.project_id,))
    assert b.dedup_key not in {k["dedup_key"] for k in keys}
    brief = worker.brief()
    assert str(b.id)[:8] not in brief
    cs = lead.get(c.id)["status"]
    assert cs["evidence_invalidated"] is True and cs["n_support"] == 0


def test_invalidating_decision_evidence_flags_verified_claim(lead: Ledger, worker: Ledger) -> None:
    first = worker.record_experiment({"v": 1}, {"score": 1}).id
    second = worker.record_experiment({"v": 2}, {"score": 2}).id
    extra = worker.record_experiment({"v": 3}, {"score": 3}).id
    assert first and second and extra
    c = worker.propose_claim("observation", "confirmed by a specific decision", evidence=[(extra, "supports")])
    lead.decide(c.id, "supported", "metric", "supported", [extra])
    lead.decide(c.id, "verified", "metric", "decision depends on different experiments", [first, second])
    lead.decide(first, "invalidated", "human", "first invalid evidence")
    assert lead.get(c.id)["status"]["evidence_invalidated"] is False
    lead.decide(second, "invalidated", "human", "second invalid evidence")
    status = lead.get(c.id)["status"]
    assert status["status"] == "verified" and status["evidence_invalidated"] is True
    assert status["n_support"] == 1
    visible = lead.sql("SELECT id FROM v_activity WHERE kind='experiment'")["rows"]
    assert visible == [{"id": str(extra)}]


@pytest.mark.parametrize("length", [53, 54, 63])
def test_valid_project_slugs_keep_writes_and_notifications(db_url: str, length: int) -> None:
    pid = (uuid.uuid4().hex * 2)[:length]
    Ledger.init_project(pid, database_url=db_url)
    worker = Ledger.open(project=pid, agent="worker", database_url=db_url)
    with psycopg.connect(db_url, autocommit=True) as listener:
        listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(notification_channel(pid))))
        recorded = worker.record_experiment({"valid": True})
        notification = next(listener.notifies(timeout=5, stop_after=1), None)
    assert recorded.id is not None and notification is not None
    assert str(recorded.id) in notification.payload


# outcome ------------------------------------------------------------------------
def test_outcome_rules(db_url: str) -> None:
    pid = make_project(db_url, metric="loss", direction="min", epsilon=0.01)
    w = Ledger.open(project=pid, agent="w", database_url=db_url)
    assert w.record_experiment({"i": 0}, {"loss": 1.0}).outcome == "improved"
    assert w.record_experiment({"i": 1}, {"loss": 0.995}).outcome == "no_change"
    assert w.record_experiment({"i": 2}, {"loss": 1.5}).outcome == "worse"
    assert w.record_experiment({"i": 3}, {"loss": 0.5}).outcome == "improved"
    assert w.record_experiment({"i": 4}, {"acc": 0.5}).outcome == "unscored"
    assert w.record_experiment({"i": 5}, {}, run_status="timeout").outcome == "failed"


def test_validation_messages(worker: Ledger, db_url: str) -> None:
    with pytest.raises(ValidationFailed) as ei:
        worker.record_experiment({"a": float("nan")}, {"score": 1})
    assert "NaN" in str(ei.value)
    with pytest.raises(ValidationFailed) as ei:
        worker.record_experiment({"a": 1}, {"score": [1, 2]}, run_status="weird", notes="x" * 2001)
    msg = str(ei.value)
    assert "run_status" in msg and "metrics.score" in msg and "notes" in msg
    deep: dict = {}
    cur = deep
    for _ in range(8):
        cur["k"] = {}
        cur = cur["k"]
    with pytest.raises(ValidationFailed, match="depth"):
        worker.record_experiment(deep, {})
    with pytest.raises(ValidationFailed, match="kind"):
        worker.propose_claim("theorem", "x")


def test_project_config_schema(db_url: str) -> None:
    schema = {"type": "object", "required": ["init"], "properties": {"init": {"enum": ["hex", "random"]}}}
    pid = make_project(db_url, config_schema=schema)
    w = Ledger.open(project=pid, agent="w", database_url=db_url)
    with pytest.raises(ValidationFailed) as ei:
        w.record_experiment({"init": "grid"}, {"score": 1})
    assert "config.init" in str(ei.value)
    assert w.record_experiment({"init": "hex"}, {"score": 1}).id


def test_validated_config_stores_original_numeric_precision(db_url: str) -> None:
    original = 1.2345678901234
    pid = make_project(db_url, config_schema={"properties": {"lr": {"const": original}}})
    worker = Ledger.open(project=pid, agent="worker", database_url=db_url)
    result = worker.record_experiment({"lr": original})
    assert result.id is not None
    assert worker.get(result.id)["record"]["config"] == {"lr": original}


def test_claim_dedup_attaches_evidence(lead: Ledger, worker: Ledger) -> None:
    w2 = lead.as_agent("worker-2", "worker")
    e1 = worker.record_experiment({"a": 1}, {"score": 1})
    e2 = w2.record_experiment({"a": 2}, {"score": 1})
    c1 = worker.propose_claim("hypothesis", "Hex init beats random.", evidence=[(e1.id, "supports")])  # type: ignore[list-item]
    c2 = w2.propose_claim("hypothesis", "  hex   INIT beats random ", evidence=[(e2.id, "supports")])  # type: ignore[list-item]
    assert c2.duplicate_of == c1.id and c2.id == c1.id
    st = lead.get(c1.id)["status"]
    assert st["n_support"] == 2 and st["n_support_authors"] == 2


def test_short_ids(lead: Ledger, worker: Ledger) -> None:
    e = worker.record_experiment({"a": 1}, {"score": 1})
    sid = f"e:{str(e.id)[:8]}"
    assert lead.resolve(sid) == ("experiment", e.id)
    assert lead.resolve(str(e.id)) == ("experiment", e.id)
    assert lead.get(sid)["record"]["id"] == str(e.id)


def test_export(lead: Ledger, worker: Ledger) -> None:
    _seed_rows(lead, worker)
    types = Counter(ev["type"] for ev in lead.export())
    assert types["project"] == 1 and types["experiment"] == 1 and types["decision"] == 1
