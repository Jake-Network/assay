"""The deterministic referee, rules T1-T7 (§10, §16.1). Runs are recorded directly; no subprocesses."""
from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from assay import Ledger
from assay.integrations.autoresearch.code import code_hash
from assay.integrations.autoresearch.ops import PROJECT_SETTINGS
from assay.integrations.autoresearch.referee import tick

PART = "sim-cpu/abcd1234"


@pytest.fixture
def pid(db_url: str) -> str:
    p = f"ref-{uuid.uuid4().hex[:10]}"
    Ledger.init_project(p, goal="Lowest val_bpb.", metric="val_bpb", direction="min", epsilon=0.0005,
                        settings=PROJECT_SETTINGS, pack="ml-research", database_url=db_url)
    return p


def agent(db_url: str, pid: str, name: str) -> Ledger:
    return Ledger.open(project=pid, agent=name, database_url=db_url)


def first_run(lg: Ledger, code: str, value: float | None, part: str = PART) -> Any:
    src = code.encode()
    uri = lg.put_blob(src)
    config = {"partition": part, "knobs": {"CODE": code}, "changed": {}, "parent": None, "description": code}
    status = "ok" if value is not None else "error"
    r = lg.record_experiment(config, {"val_bpb": value} if value is not None else {}, run_status=status, seed=0,
                             code_ref=code_hash(src), artifacts=[{"uri": uri, "kind": "train.py"}])
    assert r.id is not None
    return r


def replicate(lg: Ledger, value: float | None, part: str = PART, origin: Any = None) -> Any:
    match: dict[str, Any] = {"partition": part}
    if origin is not None:
        match["origin"] = str(origin)
    t = lg.next_task(tags=["replicate", "calibration"], spec_match=match)
    if t is None:
        return None
    origin = lg.get(t.spec["origin"])["record"]
    status = "ok" if value is not None else "error"
    lg.record_experiment(origin["config"], {"val_bpb": value} if value is not None else {}, run_status=status,
                         seed=t.spec["seed"], code_ref=t.spec["code_ref"], artifacts=origin["artifacts"],
                         task_id=t.id)
    lg.complete_task(t.id, "done")
    return t


def result_status(lg: Ledger, origin: Any) -> str | None:
    rows = lg.sql_internal("SELECT s.status FROM assay.links l JOIN assay.v_claim_status s ON s.id=l.dst_id"
                           " WHERE l.src_id=%s AND l.relation='supports' AND s.kind='result'", (origin,))
    return rows[0]["status"] if rows else None


def tasks(lg: Ledger, tag: str, status: str | None = None) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT * FROM assay.tasks WHERE project_id=%s AND %s = ANY(tags)"
                           " AND (%s::text IS NULL OR status=%s)", (lg.project_id, tag, status, status))


def calibrated_lab(db_url: str, pid: str, base: float = 1.0) -> tuple[Ledger, Ledger, Ledger, Any]:
    a, b, c = (agent(db_url, pid, n) for n in ("a", "b", "c"))
    e0 = first_run(a, "baseline", base)
    tick(a)
    assert replicate(b, base + 0.002) is not None
    assert replicate(c, base - 0.002) is not None
    tick(b)
    return a, b, c, e0


def test_t1_t2_calibration_and_baseline(db_url: str, pid: str) -> None:
    a, b, c = (agent(db_url, pid, n) for n in ("a", "b", "c"))
    e0 = first_run(a, "baseline", 1.0)
    for _ in range(10):
        tick(a)
    cal = tasks(a, "calibration")
    assert len(cal) == 2 and all(t["spec"]["exclude_agents"] == ["a"] for t in cal)
    assert {t["spec"]["seed"] for t in cal} == {1, 2} and all(t["priority"] == 20 for t in cal)
    assert replicate(a, 1.0) is None  # the first runner never replicates its own run (C4)
    replicate(b, 1.002)
    tick(b)
    assert PART not in (b.refresh().settings.get("partitions") or {})
    replicate(c, 0.998)
    tick(c)
    entry = c.refresh().settings["partitions"][PART]
    assert entry["sigma"] == pytest.approx(0.002) and entry["epsilon"] == pytest.approx(0.004)
    assert entry["n"] == 3 and entry["status"] == "ok"
    assert len(c.search(kind="calibration")) == 1
    assert result_status(a, e0.id) == "verified"
    best = a.sql_internal("SELECT * FROM assay.v_partition_best WHERE project_id=%s AND partition=%s"
                          " AND status='verified'", (pid, PART))
    assert len(best) == 1 and best[0]["mean"] == pytest.approx(1.0) and best[0]["n"] == 3


def test_t3_t5_candidate_verified(db_url: str, pid: str) -> None:
    a, b, c, _ = calibrated_lab(db_url, pid)
    e1 = first_run(b, "better", 0.990)
    assert e1.outcome == "improved"
    tick(b)
    reps = tasks(b, "replicate")
    assert len(reps) == 2 and all(t["spec"]["exclude_agents"] == ["b"] and t["priority"] == 10 for t in reps)
    assert replicate(b, 0.99) is None
    replicate(a, 0.991)
    replicate(c, 0.989)
    tick(c)
    assert result_status(a, e1.id) == "verified"
    best = a.sql_internal("SELECT * FROM assay.v_partition_best WHERE project_id=%s AND partition=%s"
                          " AND status='verified' AND rank=1", (pid, PART))[0]
    assert best["mean"] == pytest.approx(0.990) and best["author"] == "b"
    decisions = a.get(best["ref_id"])["decisions"]
    assert [(d["status"], d["oracle"], d["by"]) for d in decisions] == [("supported", "metric", "referee"),
                                                                       ("verified", "rerun", "referee")]
    # a later run is compared with the verified mean (0.990), not with the raw best
    assert first_run(c, "close", 0.988).outcome == "no_change"


def test_lucky_run_is_refuted(db_url: str, pid: str) -> None:
    a, b, c, _ = calibrated_lab(db_url, pid)
    lucky = first_run(a, "lucky", 0.985)  # true value ~0.999: one lucky run
    tick(a)
    replicate(b, 0.999)
    replicate(c, 0.998)
    tick(c)
    assert result_status(a, lucky.id) == "refuted"
    assert a.sql_internal("SELECT mean FROM assay.v_partition_best WHERE project_id=%s AND partition=%s"
                          " AND status='verified' AND rank=1", (pid, PART))[0]["mean"] == pytest.approx(1.0)


def test_lucky_first_run_cannot_carry_the_mean(db_url: str, pid: str) -> None:
    a, b, c, _ = calibrated_lab(db_url, pid)
    # mean of all three (0.9937) beats 1.0 - 0.004, but the replications alone (0.999) do not
    lucky = first_run(a, "lucky-big", 0.983)
    tick(a)
    replicate(b, 0.999)
    replicate(c, 0.999)
    tick(c)
    assert result_status(a, lucky.id) == "refuted"


def test_t6_failed_replication_is_inconclusive(db_url: str, pid: str) -> None:
    a, b, c, _ = calibrated_lab(db_url, pid)
    e = first_run(a, "flaky", 0.99)
    tick(a)
    replicate(b, None)  # crashed
    replicate(c, 0.99)
    tick(c)
    assert result_status(a, e.id) == "inconclusive"
    e2 = first_run(a, "abandoned", 0.99)
    tick(a)
    t = b.next_task(tags=["replicate"])
    assert t is not None
    b.complete_task(t.id, "failed", "gave up")
    replicate(c, 0.99)
    tick(c)
    assert result_status(a, e2.id) == "inconclusive"


def test_t4_suspicious_and_t7_pruning(db_url: str, pid: str) -> None:
    a, b, c, _ = calibrated_lab(db_url, pid)
    weak = first_run(c, "weak", 0.995)  # improved over 1.0 (epsilon 0.004) but about to be beaten
    strong = first_run(a, "strong", 0.97)
    tick(a)
    assert len(tasks(a, "replicate", "open")) == 4
    d = agent(db_url, pid, "d")
    assert replicate(b, 0.971, origin=strong.id) is not None
    assert replicate(d, 0.969, origin=strong.id) is not None
    tick(d)
    assert result_status(a, strong.id) == "verified"
    weak_tasks = [x for x in tasks(a, "replicate") if x["spec"]["origin"] == str(weak.id)]
    assert {x["status"] for x in weak_tasks} == {"cancelled"}  # T7: 0.995 cannot beat 0.97 + epsilon
    assert result_status(a, weak.id) == "inconclusive"
    sus = first_run(b, "too-good", 0.4)
    tick(b)
    obs = b.search(kind="observation")
    assert any("Suspicious" in o["line"] and str(sus.id)[:8] in o["line"] for o in obs)
    assert len(tasks(a, "replicate", "open")) == 2  # still replicated like any candidate


def test_idempotent_and_concurrent(db_url: str, pid: str) -> None:
    a, b, c, _ = calibrated_lab(db_url, pid)
    for i in range(5):
        first_run(a, f"cand{i}", 0.99 - i * 0.001)
    errors: list[Exception] = []

    def run() -> None:
        try:
            tick(agent(db_url, pid, "a"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    before = (len(tasks(a, "replicate")), a.sql_internal("SELECT count(*) AS n FROM assay.decisions")[0]["n"])
    for _ in range(10):
        tick(a)
    after = (len(tasks(a, "replicate")), a.sql_internal("SELECT count(*) AS n FROM assay.decisions")[0]["n"])
    assert before == after
    origins = [t["spec"]["origin"] for t in tasks(a, "replicate")]
    assert len(origins) == len(set(origins)) * 2
    # queue limit (4 candidates): each better arrival pushed out the weakest idle one (cand0, 0.99)
    open_origins = {t["spec"]["origin"] for t in tasks(a, "replicate", "open")}
    assert len(open_origins) == 4
    dropped = [o for o in set(origins) if o not in open_origins]
    assert len(dropped) == 1 and result_status(a, dropped[0]) == "inconclusive"
    assert a.get(dropped[0])["record"]["primary_value"] == pytest.approx(0.99)
