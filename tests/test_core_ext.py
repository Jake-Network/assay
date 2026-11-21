"""Step 3 core extensions C1-C6 (§7, §16.1)."""
from __future__ import annotations

import time
import uuid

import psycopg
import pytest

from benchbook import Ledger
from benchbook.errors import BenchbookError, Conflict, NotFound, PermissionDenied, ValidationFailed
from benchbook.integrations.autoresearch.ops import PROJECT_SETTINGS


def ml_project(db_url: str, **settings: object) -> str:
    pid = f"ml-{uuid.uuid4().hex[:10]}"
    Ledger.init_project(pid, goal="Lowest val_bpb.", metric="val_bpb", direction="min", epsilon=0.0005,
                        settings={**PROJECT_SETTINGS, **settings}, pack="ml-research", database_url=db_url)
    return pid


def cfg(part: str, code: str = "a", **extra: object) -> dict[str, object]:
    return {"partition": part, "knobs": {"K": code}, "changed": {}, "parent": None, "description": code, **extra}


@pytest.fixture
def ml(db_url: str) -> str:
    return ml_project(db_url)


def op(db_url: str, pid: str, agent: str, role: str | None = None) -> Ledger:
    return Ledger.open(project=pid, agent=agent, role=role, database_url=db_url)


# ------------------------------------------------------------------ C1 partitions and settings

def test_partitions_are_separate(db_url: str, ml: str) -> None:
    w = op(db_url, ml, "w1")
    assert w.record_experiment(cfg("h100/aaaa"), {"val_bpb": 1.0}, code_ref="c1").outcome == "improved"
    assert w.record_experiment(cfg("a100/aaaa"), {"val_bpb": 0.5}, code_ref="c2").outcome == "improved"
    # 0.9 beats h100's 1.0 although a100 has a better 0.5
    assert w.record_experiment(cfg("h100/aaaa", "b"), {"val_bpb": 0.9}, code_ref="c3").outcome == "improved"
    # the same code in another partition is not a duplicate
    r = w.record_experiment(cfg("a100/aaaa"), {"val_bpb": 0.6}, code_ref="c1")
    assert r.duplicate_of is None and r.outcome == "worse"
    best = {r["partition"]: r["primary_value"] for r in w.sql_internal(
        "SELECT partition, primary_value FROM benchbook.v_best WHERE project_id=%s", (ml,))}
    assert best == {"h100/aaaa": 0.9, "a100/aaaa": 0.5}
    assert w.record_experiment(cfg("h100/aaaa"), {"val_bpb": 0.1}, code_ref="c1").duplicate_of is not None


def test_partition_epsilon_and_settings_audit(db_url: str, ml: str) -> None:
    w, lead = op(db_url, ml, "w1"), op(db_url, ml, "lead")
    with pytest.raises(PermissionDenied):
        w.update_settings({"partitions": {"p/1": {"epsilon": 0.1}}})
    new = lead.update_settings({"partitions": {"p/1": {"epsilon": 0.1, "sigma": 0.05}}, "replicates": None})
    assert new["partitions"]["p/1"] == {"epsilon": 0.1, "sigma": 0.05} and "replicates" not in new
    lead.update_settings({"partitions": {"p/1": {"sigma": None}}})
    assert lead.project.settings["partitions"]["p/1"] == {"epsilon": 0.1}
    w.record_experiment(cfg("p/1"), {"val_bpb": 1.0}, code_ref="a")
    assert w.record_experiment(cfg("p/1", "b"), {"val_bpb": 0.95}, code_ref="b").outcome == "no_change"
    # 0.95 is now the raw best (v_best ranks raw values; DECISIONS.md): improving needs < 0.85
    assert w.record_experiment(cfg("p/1", "c"), {"val_bpb": 0.84}, code_ref="c").outcome == "improved"
    assert w.record_experiment(cfg("p/2", "c"), {"val_bpb": 0.85}, code_ref="c").outcome == "improved"
    assert w.record_experiment(cfg("p/2", "d"), {"val_bpb": 0.849}, code_ref="d").outcome == "improved"
    assert w.record_experiment(cfg("p/2", "e"), {"val_bpb": 0.8488}, code_ref="e").outcome == "no_change"
    events = lead.sql_internal("SELECT patch FROM benchbook.settings_events WHERE project_id=%s ORDER BY id", (ml,))
    assert len(events) == 2
    with psycopg.connect(db_url) as c, pytest.raises(psycopg.errors.RestrictViolation):
        c.execute("DELETE FROM benchbook.settings_events WHERE project_id=%s", (ml,))


def test_outcome_against_verified_best(db_url: str, ml: str) -> None:
    w, lead = op(db_url, ml, "w1"), op(db_url, ml, "lead")
    e = w.record_experiment(cfg("p/1"), {"val_bpb": 1.0}, code_ref="a").id
    lucky = w.record_experiment(cfg("p/1", "b"), {"val_bpb": 0.90}, code_ref="b").id
    assert lucky is not None
    # nothing verified: the raw best (the lucky 0.90) is the baseline
    assert w.record_experiment(cfg("p/1", "c"), {"val_bpb": 0.95}, code_ref="c").outcome == "worse"
    c = lead.propose_claim("result", "a replicates at 1.0", payload={
        "partition": "p/1", "code_ref": "a", "experiment_ids": [str(e)], "mean": 0.97, "std": 0.0, "n": 3,
        "origin": str(e)}, evidence=[(e, "supports")])  # type: ignore[list-item]
    lead.decide(c.id, "verified", "human", "checked by hand", [e], force=True)  # type: ignore[list-item]
    assert w.baseline("p/1") == (0.97, 0.0005)
    assert w.record_experiment(cfg("p/1", "d"), {"val_bpb": 0.95}, code_ref="d").outcome == "improved"


# ------------------------------------------------------------------ C2 blobs

def test_blobs(db_url: str, ml: str) -> None:
    w = op(db_url, ml, "w1")
    uri = w.put_blob(b"print(1)\n", "text/x-python")
    assert uri.startswith("benchbook://blob/") and w.put_blob(b"print(1)\n") == uri
    assert w.get_blob(uri) == b"print(1)\n" and w.get_blob(uri.rsplit("/", 1)[1]) == b"print(1)\n"
    with pytest.raises(NotFound):
        w.get_blob("benchbook://blob/" + "0" * 64)
    with pytest.raises(ValidationFailed):
        w.put_blob(b"x" * 1_000_001)
    other = op(db_url, ml_project(db_url), "w1")
    with pytest.raises(NotFound):
        other.get_blob(uri)
    with psycopg.connect(db_url) as c, pytest.raises(psycopg.errors.RestrictViolation):
        c.execute("UPDATE benchbook.blobs SET media_type='x' WHERE project_id=%s", (ml,))
    with psycopg.connect(db_url) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO benchbook.blobs(project_id, sha256, bytes, size, author) VALUES (%s,%s,%s,1,'w')",
                  (ml, "1" * 64, b"x"))


# ------------------------------------------------------------------ C3 intents

def test_intents(db_url: str, ml: str) -> None:
    a, b = op(db_url, ml, "a"), op(db_url, ml, "b")
    t = a.create_intent("raise MATRIX_LR", {"knob_key": "p|lr", "partition": "p"}, exclusive=["knob_key"])
    assert t.status == "claimed" and t.lease_owner == "a" and "intent" in t.tags
    with pytest.raises(Conflict) as err:
        b.create_intent("raise lr too", {"knob_key": "p|lr"}, exclusive=["knob_key"])
    assert err.value.agent == "a"
    b.create_intent("something else", {"knob_key": "p|depth"}, exclusive=["knob_key"])
    assert [x.id for x in a.my_intents()] == [t.id]
    a.update_intent(t.id, {"code_key": "p|abc"}, lease_seconds=1, exclusive=["code_key"])
    with pytest.raises(Conflict):
        b.update_intent(b.my_intents()[0].id, {"code_key": "p|abc"}, exclusive=["code_key"])
    time.sleep(1.2)
    b.create_intent("raise lr after the lease ran out", {"knob_key": "p|lr"}, exclusive=["knob_key"])
    inflight = a.sql_internal("SELECT agent FROM benchbook.v_inflight WHERE project_id=%s AND is_intent", (ml,))
    assert sorted(r["agent"] for r in inflight) == ["b", "b"]


def test_intents_need_the_setting(db_url: str) -> None:
    pid = ml_project(db_url, worker_intents=False)
    with pytest.raises(PermissionDenied):
        op(db_url, pid, "w").create_intent("x")
    op(db_url, pid, "lead").create_intent("orchestrators may")


# ------------------------------------------------------------------ C4 exclusion, spec match

def test_next_task_excludes_and_matches(db_url: str, ml: str) -> None:
    lead = op(db_url, ml, "lead")
    lead.create_tasks([{"title": "rerun", "spec": {"exclude_agents": ["a"], "partition": "p/1"}, "tags": ["replicate"]},
                       {"title": "other", "spec": {"partition": "p/2"}, "tags": ["replicate"]}])
    a, b = op(db_url, ml, "a"), op(db_url, ml, "b")
    assert a.next_task(tags=["replicate"], spec_match={"partition": "p/1"}) is None
    t = b.next_task(tags=["replicate"], spec_match={"partition": "p/1"})
    assert t is not None and t.title == "rerun"
    t2 = a.next_task(tags=["replicate"])
    assert t2 is not None and t2.title == "other"


# ------------------------------------------------------------------ C5 views, C6 pack

def test_views_visible_to_reader(db_url: str, ml: str) -> None:
    lead = op(db_url, ml, "lead")
    for v in ("v_partition_best", "v_lineage", "v_inflight", "v_knob_history", "v_best", "v_tried", "v_progress"):
        assert "rows" in lead.sql(f"SELECT * FROM {v}"), v
    w = op(db_url, ml, "w")
    e1 = w.record_experiment(cfg("p/1"), {"val_bpb": 1.0}, code_ref="a").id
    c2 = cfg("p/1", "b", changed={"K": ["a", "b"]}, parent="a")
    e2 = w.record_experiment(c2, {"val_bpb": 0.9}, code_ref="b").id
    c3 = cfg("p/1", "c", changed={"K": ["b", "c"]}, parent="b")
    e3 = w.record_experiment(c3, {"val_bpb": 0.95}, code_ref="c").id
    w.link(e2, "derived_from", e1)  # type: ignore[arg-type]
    w.link(e3, "derived_from", e2)  # type: ignore[arg-type]
    lin = {r["id"]: r for r in lead.sql("SELECT id, parent_id, depth FROM v_lineage")["rows"]}
    assert lin[str(e3)]["depth"] == 2 and lin[str(e3)]["parent_id"] == str(e2)
    kh = lead.sql("SELECT knob, value, n_improved, n_worse FROM v_knob_history ORDER BY value")["rows"]
    assert kh == [{"knob": "K", "value": "b", "n_improved": 1, "n_worse": 0},
                  {"knob": "K", "value": "c", "n_improved": 0, "n_worse": 1}]
    cands = lead.sql("SELECT ref_id, rank FROM v_partition_best WHERE status='candidate' ORDER BY rank")["rows"]
    assert [c["ref_id"] for c in cands] == [str(e2), str(e1)]


def test_ml_research_pack_validation(db_url: str, ml: str) -> None:
    w = op(db_url, ml, "w")
    with pytest.raises(ValidationFailed):
        w.record_experiment({"knobs": {}}, {"val_bpb": 1.0}, code_ref="a")  # no partition
    with pytest.raises(ValidationFailed):
        w.record_experiment(cfg("p"), {"loss": 1.0}, code_ref="a")  # ok run without val_bpb
    with pytest.raises(ValidationFailed):
        w.record_experiment(cfg("p"), {"val_bpb": "low"}, code_ref="a")
    with pytest.raises(ValidationFailed):
        w.record_experiment(cfg("p", changed={"K": [1]}), {"val_bpb": 1.0}, code_ref="a")
    assert w.record_experiment(cfg("p"), {}, run_status="error", code_ref="a").outcome == "failed"
    first = w.record_experiment(cfg("p", description="first wording"), {"val_bpb": 1.0}, code_ref="a")
    again = w.record_experiment(cfg("p", description="other wording", parent="z", changed={"K": [1, 2]}),
                                {"val_bpb": 1.0}, code_ref="a")
    assert again.duplicate_of == first.id  # dedup ignores lineage and wording
    with pytest.raises(ValidationFailed):
        w.propose_claim("result", "missing payload fields", payload={"partition": "p"})
    with pytest.raises(BenchbookError, match="unknown pack"):
        Ledger.init_project("bad-pack", pack="nope", database_url=db_url)
