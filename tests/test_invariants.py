"""§18.1 invariant tests I1-I7, and I8: identity on a benchbook_writer connection (migration 0008)."""
from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from benchbook import Ledger, identity
from benchbook.db.conn import notification_channel
from benchbook.errors import InvalidTransition, LeaseLost, PermissionDenied, ValidationFailed

from .conftest import make_project, sleep_past_lease
from .writer import as_writer, issue, token_ledger, writer_conn

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
    sleep_past_lease(db_url)
    t2 = b.next_task(lease_seconds=1)
    assert t2 is not None and t2.id == t1.id and t2.attempts == 2 and t2.lease_owner == "b"
    with pytest.raises(LeaseLost):
        a.heartbeat(t1.id)
    with pytest.raises(LeaseLost):
        a.complete_task(t1.id, "done", "")
    sleep_past_lease(db_url)
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


# I8: identity on a benchbook_writer connection (migration 0008) -----------------------------------------
# The attacker in these tests holds the writer login and, at most, its own worker token. It talks to the
# database directly, so nothing the SDK checks stands in its way.
_RAW_WRITES: dict[str, tuple[str, str]] = {
    # table: (the column naming the writer, an INSERT whose %(who)s is that column)
    "experiments": ("author", "INSERT INTO benchbook.experiments(project_id,author,config,dedup_key,run_status,outcome)"
                              " VALUES (%(p)s,%(who)s,'{}',%(u)s,'error','failed')"),
    "claims": ("author", "INSERT INTO benchbook.claims(project_id,kind,statement,statement_hash,author)"
                         " VALUES (%(p)s,'note',%(u)s,%(u)s,%(who)s)"),
    "sources": ("author", "INSERT INTO benchbook.sources(project_id,uri,author) VALUES (%(p)s,%(u)s,%(who)s)"),
    "links": ("author", "INSERT INTO benchbook.links(project_id,src_type,src_id,relation,dst_type,dst_id,author)"
                        " VALUES (%(p)s,'claim',gen_random_uuid(),'supports','claim',%(c)s,%(who)s)"),
    "blobs": ("author", "INSERT INTO benchbook.blobs(project_id,sha256,bytes,size,author)"
                        " VALUES (%(p)s,encode(sha256(%(u)s::bytea),'hex'),%(u)s::bytea,octet_length(%(u)s::bytea),"
                        "%(who)s)"),
    "dedup_hits": ("agent_id", "INSERT INTO benchbook.dedup_hits(project_id,kind,dedup_key,agent_id)"
                               " VALUES (%(p)s,'avoided',%(u)s,%(who)s)"),
    "brief_log": ("agent_id", "INSERT INTO benchbook.brief_log(project_id,agent_id,tokens_est)"
                              " VALUES (%(p)s,%(who)s,1)"),
    "decisions": ("decided_by", "INSERT INTO benchbook.decisions(project_id,target_type,target_id,new_status,oracle,"
                                "rationale,decided_by) VALUES (%(p)s,'claim',%(c)s,'supported','metric','x',%(who)s)"),
    "settings_events": ("agent_id", "INSERT INTO benchbook.settings_events(project_id,agent_id,patch,settings)"
                                    " VALUES (%(p)s,%(who)s,'{}','{}')"),
    "task_events": ("agent_id", "INSERT INTO benchbook.task_events(project_id,task_id,event,agent_id)"
                                " VALUES (%(p)s,%(t)s,'done',%(who)s)"),
    "tasks": ("created_by", "INSERT INTO benchbook.tasks(project_id,title,created_by) VALUES (%(p)s,'forged',%(who)s)"),
}
_REFUSED = psycopg.errors.InsufficientPrivilege


class _Lab:
    """A project with a claim and a task, a lead and two workers who each hold a token."""

    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.project = make_project(db_url)
        owner_lead = Ledger.open(project=self.project, agent="lead", database_url=db_url)
        self.claim = owner_lead.propose_claim("note", "something to decide").id
        self.task = owner_lead.create_tasks([{"title": "t"}])[0]
        self.lead_token = issue(db_url, self.project, "lead")
        self.w1_token = issue(db_url, self.project, "w1", "worker")
        self.w2_token = issue(db_url, self.project, "w2", "worker")
        self.owner = owner_lead

    def raw(self, table: str, who: str) -> tuple[str, dict[str, Any]]:
        return _RAW_WRITES[table][1], {"p": self.project, "who": who, "u": uuid.uuid4().hex, "c": self.claim,
                                       "t": self.task}

    def count(self, table: str) -> int:
        return int(self.owner.sql_internal(  # noqa: S608 (table names come from this file)
            f"SELECT count(*) AS n FROM benchbook.{table} WHERE project_id = %s", (self.project,))[0]["n"])


def _refused(conn: psycopg.Connection[Any], query: str, params: Any = None, match: str | None = None) -> None:
    """The statement is refused for lack of privilege or identity; the transaction (and its identity) lives on."""
    with pytest.raises(_REFUSED, match=match):
        with conn.transaction():
            conn.execute(query, params)  # type: ignore[arg-type]


@pytest.mark.parametrize("table", sorted(_RAW_WRITES))
def test_i8_writer_without_a_token_cannot_write_under_any_name(db_url: str, table: str) -> None:
    lab = _Lab(db_url)
    before = lab.count(table)
    with writer_conn(db_url) as conn:
        for who in ("lead", "w1", "nobody-registered"):
            _refused(conn, *lab.raw(table, who), match="has not presented an agent token")
    assert lab.count(table) == before
    # and the SDK says so before any statement is tried
    with pytest.raises(PermissionDenied, match="BENCHBOOK_TOKEN"):
        Ledger.open(project=lab.project, agent="lead", database_url=as_writer(db_url))


def test_i8_a_name_in_a_session_setting_is_not_an_identity(db_url: str) -> None:
    lab = _Lab(db_url)
    lead_hash = lab.owner.sql_internal("SELECT token_hash FROM benchbook.agents WHERE project_id=%s AND id='lead'",
                                       (lab.project,))[0]["token_hash"]
    assert lead_hash == identity.token_hash(lab.lead_token) and lab.lead_token not in lead_hash
    with writer_conn(db_url) as conn:
        # any client may SET a custom setting: a name, a role or even the stored hash in it proves nothing
        for setting in ("benchbook.agent", "benchbook.token", "benchbook.role", "benchbook.identity"):
            for value in ("lead", "orchestrator", lead_hash, "bbt_" + "A" * 43):
                for scope in ("SET", "SET LOCAL"):
                    conn.execute(sql.SQL("{} {} = {}").format(sql.SQL(scope), sql.SQL(setting), sql.Literal(value)))
                    _refused(conn, *lab.raw("decisions", "lead"), match="has not presented an agent token")
                    _refused(conn, *lab.raw("experiments", "lead"), match="has not presented an agent token")
        assert conn.execute("SELECT agent_id FROM benchbook.session_identity(%s)", (lab.project,)).fetchone() == (None,)
    assert lab.count("decisions") == 0


@pytest.mark.parametrize("table", sorted(_RAW_WRITES))
def test_i8_a_token_writes_only_as_its_own_agent(db_url: str, table: str) -> None:
    lab = _Lab(db_url)
    before = lab.count(table)
    with writer_conn(db_url, lab.project, lab.w1_token) as conn:
        for who in ("lead", "w2", "human", "nobody-registered"):
            _refused(conn, *lab.raw(table, who), match="token belongs to agent w1")
        if table in ("decisions", "settings_events", "tasks"):  # lead-only: its own name is not enough either (P2)
            _refused(conn, *lab.raw(table, "w1"), match="only orchestrator or human")
        else:
            conn.execute(*lab.raw(table, "w1"))
            conn.commit()
    assert lab.count(table) == before + (0 if table in ("decisions", "settings_events", "tasks") else 1)
    with writer_conn(db_url, lab.project, lab.lead_token) as conn:
        _refused(conn, *lab.raw(table, "w1"), match="token belongs to agent lead")
        conn.execute(*lab.raw(table, "lead"))  # the lead's token carries the lead's role
        conn.commit()
    assert lab.count(table) == before + (1 if table in ("decisions", "settings_events", "tasks") else 2)


def test_i8_a_token_is_valid_for_its_project_only(db_url: str) -> None:
    lab, other = _Lab(db_url), _Lab(db_url)
    with writer_conn(db_url) as conn:
        with pytest.raises(psycopg.errors.InvalidPassword):
            conn.execute("SELECT * FROM benchbook.authenticate(%s, %s)", (other.project, lab.lead_token))
    with writer_conn(db_url, lab.project, lab.lead_token) as conn:  # the lead of one project, writing into another
        _refused(conn, *other.raw("decisions", "lead"), match="has not presented an agent token")
        _refused(conn, *other.raw("experiments", "lead"), match="has not presented an agent token")
    with writer_conn(db_url) as conn:
        for bad in ("", "bbt_", lab.lead_token[:-1], lab.lead_token + "x", identity.token_hash(lab.lead_token)):
            with pytest.raises(psycopg.errors.InvalidPassword):
                with conn.transaction():
                    conn.execute("SELECT * FROM benchbook.authenticate(%s, %s)", (lab.project, bad))


def test_i8_identity_does_not_stay_on_a_pooled_connection(db_url: str) -> None:
    lab = _Lab(db_url)
    w1 = Ledger.open(project=lab.project, database_url=as_writer(db_url), token=lab.w1_token)
    w2 = Ledger.open(project=lab.project, database_url=as_writer(db_url), token=lab.w2_token)
    assert (w1.agent, w2.agent) == ("w1", "w2")
    pool = Ledger._pool(as_writer(db_url))
    with w1._conn() as conn:
        authenticated_pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        assert conn.execute("SELECT agent_id FROM benchbook.session_identity(%s)",
                            (lab.project,)).fetchone()["agent_id"] == "w1"
    # the very connection w1 authenticated on, checked out again without a token
    for _ in range(50):
        with pool.connection() as conn:
            if conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"] != authenticated_pid:
                continue
            assert conn.execute("SELECT current_setting('benchbook.token', true) AS t").fetchone()["t"] in (None, "")
            assert conn.execute("SELECT agent_id FROM benchbook.session_identity(%s)",
                                (lab.project,)).fetchone()["agent_id"] is None
            _refused(conn, *lab.raw("experiments", "w1"), match="has not presented an agent token")
            break
    else:
        pytest.fail("the pool never handed back the connection w1 authenticated on")
    # two agents taking turns on the same pool each write under their own name, and only that
    for i in range(6):
        for lg in (w1, w2):
            assert lg.record_experiment({"i": i, "by": lg.agent}, {"score": i}).id is not None
            other = "w2" if lg.agent == "w1" else "w1"
            with pytest.raises(PermissionDenied, match=f"token belongs to agent {lg.agent}"):
                with lg._conn() as conn:
                    conn.execute(*lab.raw("experiments", other))
    rows = lab.owner.sql_internal("SELECT author, config->>'by' AS by FROM benchbook.experiments WHERE project_id=%s",
                                  (lab.project,))
    assert len(rows) == 12 and all(r["author"] == r["by"] for r in rows)


def test_i8_writer_cannot_reach_what_identity_rests_on(db_url: str) -> None:
    lab = _Lab(db_url)
    owner_role = lab.owner.sql_internal("SELECT current_user AS u")[0]["u"]
    with writer_conn(db_url, lab.project, lab.w1_token) as conn:
        assert conn.execute("SELECT session_user, current_user").fetchone() == ("benchbook_writer", "benchbook_writer")
        me = conn.execute("SELECT * FROM benchbook.session_identity(%s)", (lab.project,)).fetchone()
        assert me == ("w1", "worker")
        # the helpers the triggers call are callable, and say no more than the token already proves
        assert conn.execute("SELECT benchbook.session_trusted()").fetchone() == (False,)
        assert conn.execute("SELECT * FROM benchbook.required_identity(%s)", (lab.project,)).fetchone() == me
        for attack in (
            "SELECT token_hash FROM benchbook.agents",
            "SELECT * FROM benchbook.agents",
            "UPDATE benchbook.agents SET token_hash = repeat('0', 64) WHERE id = 'lead'",
            "UPDATE benchbook.agents SET model = 'x' WHERE id = 'w1'",
            "INSERT INTO benchbook.agents(project_id, id, role) SELECT id, 'second-lead', 'orchestrator'"
            " FROM benchbook.projects",
            "INSERT INTO benchbook.projects(id, title, goal) VALUES ('forged-project', 't', 'g')",
            "ALTER TABLE benchbook.decisions DISABLE TRIGGER ALL",
            "ALTER TABLE benchbook.experiments DISABLE TRIGGER experiments_identity",
            "DROP TRIGGER decisions_identity ON benchbook.decisions",
            "CREATE OR REPLACE FUNCTION benchbook.session_trusted() RETURNS boolean LANGUAGE sql AS 'SELECT true'",
            "CREATE FUNCTION benchbook.helper() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "ALTER FUNCTION benchbook.bind_identity() RESET search_path",
            "SET session_replication_role = replica",
            f'SET ROLE "{owner_role}"',
            "GRANT UPDATE ON benchbook.agents TO benchbook_writer",
            "UPDATE benchbook.experiments SET author = 'w1'",
            "DELETE FROM benchbook.decisions",
            "TRUNCATE benchbook.claims",
            "UPDATE benchbook.projects SET goal = 'another goal'",
            "UPDATE benchbook.tasks SET title = 'renamed', created_by = 'w1'",
            "INSERT INTO benchbook.experiments_valid(project_id,author,config,dedup_key,run_status,outcome)"
            " SELECT id, 'lead', '{}', 'k', 'error', 'failed' FROM benchbook.projects",
        ):
            _refused(conn, attack)
        # the trigger functions resolve names by their own search_path, whatever the session sets
        conn.execute("SET search_path = pg_temp, public, benchbook")
        _refused(conn, *lab.raw("decisions", "lead"), match="token belongs to agent w1")
        _refused(conn, *lab.raw("decisions", "w1"), match="only orchestrator or human")
    assert lab.count("decisions") == 0


def test_i8_a_left_out_author_is_the_tokens_agent(db_url: str) -> None:
    """The plan's "the trigger takes the author from the identity": a row without its author column gets the
    token's agent, and a lead-only row written that way still needs the lead's role."""
    lab = _Lab(db_url)
    nameless = {
        "experiments": "INSERT INTO benchbook.experiments(project_id,config,dedup_key,run_status,outcome)"
                       " VALUES (%(p)s,'{}',%(u)s,'error','failed') RETURNING author AS who",
        "dedup_hits": "INSERT INTO benchbook.dedup_hits(project_id,kind,dedup_key) VALUES (%(p)s,'avoided',%(u)s)"
                      " RETURNING agent_id AS who",
        "decisions": "INSERT INTO benchbook.decisions(project_id,target_type,target_id,new_status,oracle,rationale)"
                     " VALUES (%(p)s,'claim',%(c)s,'supported','metric','x') RETURNING decided_by AS who",
    }
    params = {"p": lab.project, "u": uuid.uuid4().hex, "c": lab.claim}
    with writer_conn(db_url, lab.project, lab.w1_token) as conn:
        assert conn.execute(nameless["experiments"], params).fetchone() == ("w1",)
        assert conn.execute(nameless["dedup_hits"], params).fetchone() == ("w1",)
        _refused(conn, nameless["decisions"], params, match="may not write decisions")
    with writer_conn(db_url, lab.project, lab.lead_token) as conn:
        assert conn.execute(nameless["decisions"], params).fetchone() == ("lead",)
    with writer_conn(db_url) as conn:  # no token: nothing to take the name from
        _refused(conn, nameless["experiments"], params, match="has not presented an agent token")
    with psycopg.connect(db_url) as conn:  # the owner's connection fills nothing in: NOT NULL, as before
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute(nameless["experiments"], params)


def test_i8_a_declared_repeat_is_bound_too(db_url: str) -> None:
    """Migration 0009's duplicate_of path (dedup_mode = "notify") goes through the same identity check, and its
    own trigger, which reads experiments as the caller, works for the writer login."""
    lab = _Lab(db_url)
    w2 = Ledger.open(project=lab.project, database_url=as_writer(db_url), token=lab.w2_token)
    original = w2.record_experiment({"a": 1}, {"score": 1.0}, seed=7)
    key = lab.owner.sql_internal("SELECT dedup_key FROM benchbook.experiments WHERE id = %s", (original.id,))[0]
    repeat = ("INSERT INTO benchbook.experiments(project_id,author,config,dedup_key,seed,run_status,outcome,"
              "duplicate_of) VALUES (%(p)s,%(who)s,'{\"a\": 1}',%(k)s,7,'ok','unscored',%(o)s) RETURNING author")
    params = {"p": lab.project, "k": key["dedup_key"], "o": original.id}
    with writer_conn(db_url, lab.project, lab.w1_token) as conn:
        for who in ("w2", "lead"):  # a repeat in the original author's name would fake an independent rerun
            _refused(conn, repeat, {**params, "who": who}, match="token belongs to agent w1")
        assert conn.execute(repeat, {**params, "who": "w1"}).fetchone() == ("w1",)
        assert conn.execute(repeat, {**params, "who": None}).fetchone() == ("w1",)
        with pytest.raises(psycopg.errors.CheckViolation):  # 0009's own rule still holds on this connection
            with conn.transaction():
                conn.execute(repeat, {**params, "who": "w1", "o": uuid.uuid4()})
        conn.commit()
    with writer_conn(db_url) as conn:
        _refused(conn, repeat, {**params, "who": "w1"}, match="has not presented an agent token")
    rows = lab.owner.sql_internal("SELECT author FROM benchbook.experiments WHERE duplicate_of = %s", (original.id,))
    assert [r["author"] for r in rows] == ["w1", "w1"]


def test_i8_copy_is_checked_like_insert(db_url: str) -> None:
    lab = _Lab(db_url)
    copy = "COPY benchbook.experiments(project_id,author,config,dedup_key,run_status,outcome) FROM STDIN"
    with writer_conn(db_url, lab.project, lab.w1_token) as conn:
        with pytest.raises(_REFUSED, match="token belongs to agent w1"):
            with conn.transaction(), conn.cursor().copy(copy) as cp:
                cp.write_row((lab.project, "lead", "{}", uuid.uuid4().hex, "error", "failed"))
    assert lab.count("experiments") == 0


def test_i8_a_login_that_is_a_member_of_the_writer_role_is_a_writer(db_url: str) -> None:
    """docs/multi-machine.md suggests one login per machine, each a member of benchbook_writer, so one machine
    cannot change the password the others use. Such a login gets the writer's rules, not the owner's."""
    lab = _Lab(db_url)
    login = "benchbook_writer_member_test"
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))
        c.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'member' IN ROLE benchbook_writer").format(
            sql.Identifier(login)))
    try:
        url = make_conninfo(db_url, user=login, password="member")
        with psycopg.connect(url) as conn:
            _refused(conn, *lab.raw("experiments", "w1"), match="has not presented an agent token")
            conn.execute("SELECT * FROM benchbook.authenticate(%s, %s)", (lab.project, lab.w1_token))
            _refused(conn, *lab.raw("decisions", "lead"), match="token belongs to agent w1")
            _refused(conn, "SELECT token_hash FROM benchbook.agents")
            conn.execute(*lab.raw("experiments", "w1"))
        w1 = Ledger.open(project=lab.project, database_url=url, token=lab.w1_token)
        assert w1.record_experiment({"via": "member login"}).id is not None
        with pytest.raises(PermissionDenied):
            Ledger.open(project=lab.project, agent="lead", database_url=url)
    finally:
        pool = Ledger._pools.pop(url, None)
        if pool is not None:
            pool.close()
        with psycopg.connect(db_url, autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s", (login,))
            c.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))


def test_i8_writer_role_holds_insert_and_read_only(db_url: str) -> None:
    """Pins the grants of benchbook_writer. A migration that adds a table or view fails here until it decides
    what the writer may do with it (copy the grants in 0008_agent_tokens.sql)."""
    with psycopg.connect(db_url) as conn:
        role = conn.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication FROM pg_roles"
                            " WHERE rolname = 'benchbook_writer'").fetchone()
        assert role == (False, False, False, False, False)
        assert conn.execute("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
                            " WHERE r.rolname = 'benchbook_writer'").fetchone() == (0,)
        rels = conn.execute(
            "SELECT c.relname, c.relkind::text, array_agg(p.priv) FILTER (WHERE"
            " has_table_privilege('benchbook_writer', c.oid, p.priv)) FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p(priv)"
            " WHERE n.nspname = 'benchbook' AND c.relkind IN ('r','v','p','m') GROUP BY 1, 2").fetchall()
        cols = conn.execute(
            "SELECT c.relname || '.' || a.attname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped"
            " WHERE n.nspname = 'benchbook' AND c.relkind = 'r'"
            " AND has_column_privilege('benchbook_writer', c.oid, a.attnum, 'UPDATE') ORDER BY 1").fetchall()
        agent_cols = conn.execute(
            "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = 'benchbook.agents'::regclass AND a.attnum > 0"
            " AND NOT a.attisdropped AND NOT has_column_privilege('benchbook_writer', a.attrelid, a.attnum, 'SELECT')"
        ).fetchall()
        schema_create = conn.execute("SELECT has_schema_privilege('benchbook_writer', 'benchbook', 'CREATE'),"
                                     " has_schema_privilege('benchbook_writer', 'benchbook_q', 'USAGE')").fetchone()
    insertable = {"experiments", "claims", "sources", "links", "decisions", "blobs", "tasks", "task_events",
                  "dedup_hits", "brief_log", "settings_events"}
    assert len(rels) > 20
    for name, kind, privs in rels:
        expected = {"SELECT"} if name != "agents" else set()
        if kind == "r" and name in insertable:
            expected.add("INSERT")
        assert set(privs or []) == expected, name
    assert insertable <= {name for name, kind, _ in rels if kind == "r"}
    # the only columns it may update: the lease state of the task queue and the project settings
    assert [c for (c,) in cols] == ["projects.settings", "tasks.attempts", "tasks.lease_expires_at",
                                    "tasks.lease_owner", "tasks.spec", "tasks.status", "tasks.summary",
                                    "tasks.updated_at"]
    assert agent_cols == [("token_hash",)]
    assert schema_create == (False, False)


def test_i8_lead_only_actions_need_the_leads_token(db_url: str) -> None:
    lab = _Lab(db_url)
    w1 = Ledger.open(project=lab.project, database_url=as_writer(db_url), token=lab.w1_token)
    lead = Ledger.open(project=lab.project, database_url=as_writer(db_url), token=lab.lead_token)
    assert (w1.agent, w1.role, lead.agent, lead.role) == ("w1", "worker", "lead", "orchestrator")
    e = w1.record_experiment({"a": 1}, {"score": 1.0})
    for forbidden in (lambda: w1.decide(lab.claim, "supported", "metric", "self-approval", [e.id]),  # type: ignore[list-item]
                      lambda: w1.update_settings({"rerun_min_authors": 1}),
                      lambda: w1.create_tasks([{"title": "x"}]),
                      lambda: w1.cancel_task(lab.task, "not mine to cancel"),
                      lambda: w1.as_agent("lead"),
                      lambda: Ledger.open(project=lab.project, agent="lead", database_url=as_writer(db_url),
                                          token=lab.w1_token)):
        with pytest.raises(PermissionDenied):
            forbidden()
    with writer_conn(db_url, lab.project, lab.w1_token) as conn:  # the same, past the SDK
        _refused(conn, "UPDATE benchbook.projects SET settings = settings || '{\"rerun_min_authors\": 1}'"
                       " WHERE id = %s", (lab.project,), match="may not change project settings")
        _refused(conn, "UPDATE benchbook.tasks SET status = 'cancelled' WHERE id = %s", (lab.task,),
                 match="may not change task")
    assert lab.count("decisions") == 0 and lab.count("settings_events") == 0
    assert lead.decide(lab.claim, "supported", "metric", "measured", [e.id]).decided_by == "lead"  # type: ignore[list-item]
    assert lead.update_settings({"rerun_min_authors": 1})["rerun_min_authors"] == 1
    assert lead.cancel_task(lead.create_tasks([{"title": "y"}])[0], "changed plan").status == "cancelled"


def test_i8_task_changes_are_tied_to_the_holder(db_url: str) -> None:
    lab = _Lab(db_url)
    w1 = Ledger.open(project=lab.project, database_url=as_writer(db_url), token=lab.w1_token)
    [spare] = lab.owner.create_tasks([{"title": "spare", "spec": {"exclude_agents": ["w2"]}}])
    held = w1.next_task(task_id=lab.task)
    assert held is not None and held.lease_owner == "w1"
    with writer_conn(db_url, lab.project, lab.w2_token) as conn:
        for attack, params in (
            # take over a live lease
            ("UPDATE benchbook.tasks SET lease_owner = 'w2', attempts = attempts + 1,"
             " lease_expires_at = now() + interval '1 hour' WHERE id = %s", (lab.task,)),
            # finish, extend or release somebody else's task
            ("UPDATE benchbook.tasks SET status = 'done', summary = 'forged', lease_expires_at = NULL WHERE id = %s",
             (lab.task,)),
            ("UPDATE benchbook.tasks SET lease_expires_at = now() - interval '1 hour' WHERE id = %s", (lab.task,)),
            ("UPDATE benchbook.tasks SET status = 'failed' WHERE id = %s", (lab.task,)),
            # claim in another agent's name, or rewrite a task while claiming it
            ("UPDATE benchbook.tasks SET status = 'claimed', lease_owner = 'w1', attempts = attempts + 1,"
             " lease_expires_at = now() + interval '1 hour' WHERE id = %s", (spare,)),
            ("UPDATE benchbook.tasks SET status = 'claimed', lease_owner = 'w2', attempts = attempts + 1,"
             " lease_expires_at = now() + interval '1 hour', spec = '{}' WHERE id = %s", (spare,)),
            ("UPDATE benchbook.tasks SET spec = '{}' WHERE id = %s", (spare,)),
            ("UPDATE benchbook.tasks SET attempts = 0 WHERE id = %s", (lab.task,)),
            # an open task, or an intent held by someone else, in its own name
            ("INSERT INTO benchbook.tasks(project_id,title,created_by) VALUES (%s,'forged','w2')", (lab.project,)),
            ("INSERT INTO benchbook.tasks(project_id,title,created_by,tags,status,lease_owner,lease_expires_at)"
             " VALUES (%s,'forged','w2','{intent}','claimed','w1',now() + interval '1 hour')", (lab.project,)),
        ):
            _refused(conn, attack, params, match="may not (change task|create this task)")
        # events in the holder's name
        for event in ("claimed", "heartbeat", "done", "failed", "expired", "cancelled"):
            _refused(conn, "INSERT INTO benchbook.task_events(project_id,task_id,event,agent_id)"
                           " VALUES (%s,%s,%s,'w1')", (lab.project, lab.task, event), match="token belongs to agent w2")
    assert w1.get_task(lab.task).lease_owner == "w1" and w1.get_task(spare).spec == {"exclude_agents": ["w2"]}
    assert [ev["event"] for ev in lab.owner.get(lab.task)["events"]] == ["created", "claimed"]
    assert w1.complete_task(lab.task, "done", "finished by its holder").status == "done"


def _run_out_lease(db_url: str, task: Any) -> None:
    with psycopg.connect(db_url) as conn:
        conn.execute("UPDATE benchbook.tasks SET lease_expires_at = clock_timestamp() - interval '1 second'"
                     " WHERE id = %s", (task,))


def test_i8_sdk_works_end_to_end_over_the_writer_login(db_url: str) -> None:
    """Everything a lead and its workers do through the SDK, with only the writer login and their tokens."""
    pid = make_project(db_url)
    lead = token_ledger(db_url, pid, "lead")
    a, b = token_ledger(db_url, pid, "a", "worker"), token_ledger(db_url, pid, "b", "worker")
    _seed_rows(lead, a)
    lead.update_settings({"worker_intents": True})
    intent = a.create_intent("trying lr=3", {"knob": "lr"}, exclusive=["knob"])
    assert a.update_intent(intent.id, {"value": 3}).spec == {"knob": "lr", "value": 3}
    assert [t.id for t in a.my_intents()] == [intent.id]
    assert a.complete_task(intent.id, "done", "").status == "done"
    uri = a.put_blob(b"patch")
    assert b.get_blob(uri) == b"patch" and b.put_blob(b"patch") == uri
    src = b.add_source("https://example.org/paper")
    e = b.record_experiment({"a": 2}, {"score": 2.0})
    c = b.propose_claim("observation", "a=2 scores 2", evidence=[(e.id, "supports")])  # type: ignore[list-item]
    b.link(src, "supports", c.id)
    assert b.record_experiment({"a": 2}, {"score": 2.0}).duplicate_of == e.id
    assert lead.decide(e.id, "invalidated", "human", "evaluator bug").decided_by == "lead"  # type: ignore[arg-type]
    assert lead.status()["dedup"]["rejected"] == 1 and "a=2" not in b.brief()
    # leases: expiry, takeover and exhaustion write events in the previous holder's name (test_i2, with tokens)
    [t] = lead.create_tasks([{"title": "x", "max_attempts": 2}])
    t1 = a.next_task()
    assert t1 is not None and t1.id == t and b.next_task() is None
    assert a.heartbeat(t) is not None
    _run_out_lease(db_url, t)  # on the owner's connection: a clock jump in the test VM cannot make this flaky
    t2 = b.next_task()
    assert t2 is not None and t2.id == t and t2.lease_owner == "b"
    with pytest.raises(LeaseLost):
        a.complete_task(t, "done", "")
    _run_out_lease(db_url, t)
    assert a.next_task() is None and lead.get_task(t).status == "failed"
    events = [(ev["event"], ev["agent_id"]) for ev in lead.get(t)["events"]]
    assert events == [("created", "lead"), ("claimed", "a"), ("heartbeat", "a"), ("expired", "a"), ("claimed", "b"),
                      ("expired", "b"), ("failed", "b")]
    exported = list(lead.export())
    assert Counter(ev["type"] for ev in exported)["experiment"] == 2
    agents = [ev["record"] for ev in exported if ev["type"] == "agent"]
    assert {r["id"] for r in agents} >= {"lead", "a", "b"} and all("token_hash" not in r for r in agents)
    authors = lead.sql_internal("SELECT DISTINCT author FROM benchbook.experiments WHERE project_id=%s", (pid,))
    assert {r["author"] for r in authors} == {"a", "b"}


def test_i8_token_never_appears_in_statement_text(db_url: str) -> None:
    """Every agent shares the writer login, and pg_stat_activity shows a login its own sessions' statements."""
    lab = _Lab(db_url)
    w1 = Ledger.open(project=lab.project, database_url=as_writer(db_url), token=lab.w1_token)
    with w1._conn() as conn, writer_conn(db_url, lab.project, lab.w2_token) as spy:
        pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        conn.execute("SELECT * FROM benchbook.authenticate(%s, %s)", (lab.project, lab.w1_token))
        seen = spy.execute("SELECT query FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()
        assert seen is not None and "authenticate" in seen[0] and lab.w1_token not in seen[0]
        # nor can another session read the setting that carries it
        assert spy.execute("SELECT current_setting('benchbook.token', true)").fetchone() != (lab.w1_token,)


def test_i8_owner_connection_is_unchanged_by_tokens(db_url: str) -> None:
    """Local mode: the owner's connection needs no token, also for an agent that has one."""
    lab = _Lab(db_url)
    assert lab.owner.as_agent("w1").record_experiment({"a": 1}, {"score": 1.0}).id is not None
    assert Ledger.open(project=lab.project, agent="fresh-name", database_url=db_url).role == "worker"
    with psycopg.connect(db_url) as conn:
        conn.execute(*lab.raw("experiments", "w2"))
        conn.execute(*lab.raw("decisions", "lead"))
        with pytest.raises(_REFUSED, match="may not write decisions"):  # the role check is as it was (I6)
            conn.execute(*lab.raw("decisions", "w2"))
