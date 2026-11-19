"""Brief compiler (§8): format, budget, determinism, ids exist (I5)."""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from assay import Ledger
from assay.brief import compile_brief
from assay.errors import ValidationFailed
from assay.ids import short
from assay.tokens import estimate_tokens

from .conftest import make_project

ID_RE = re.compile(r"\b([ectsd]):([0-9a-f]{8})\b")
NOW = datetime(2026, 11, 19, 10, 2, tzinfo=UTC)


def test_token_estimate() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("한국어") == 3
    assert estimate_tokens("ab한") == 2


def test_brief_sections(lead: Ledger, worker: Ledger) -> None:
    [tid] = lead.create_tasks([{"title": "Try hex init", "tags": ["init"],
                                "spec": {"config_space": {"init": ["hex", "random"], "optimizer": ["slsqp"]}}}])
    t = worker.next_task()
    assert t is not None
    ids = []
    for s in range(3):
        ids.append(worker.record_experiment({"init": "hex", "optimizer": "slsqp"}, {"score": 2.0 + s / 100},
                                            seed=s, task_id=tid).id)
    worker.record_experiment({"init": "random", "optimizer": "lbfgs"}, {}, run_status="error",
                             notes="overlap violations", task_id=tid)
    w2 = lead.as_agent("worker-2")
    ids.append(w2.record_experiment({"init": "hex", "optimizer": "slsqp"}, {"score": 2.0}, seed=9).id)
    c = worker.propose_claim("hypothesis", "SLSQP polish helps", evidence=[(i, "supports") for i in ids])  # type: ignore[misc]
    lead.decide(c.id, "supported", "metric", "ok", ids)  # type: ignore[arg-type]
    lead.decide(c.id, "verified", "rerun", "ok", ids)  # type: ignore[arg-type]
    c2 = worker.propose_claim("hypothesis", "Perturbation 0.02 beats 0.05")
    lead.decide(c2.id, "supported", "llm_judge", "plausible")
    worker.propose_claim("hypothesis", "Boundary circles first")
    b = worker.brief(task_id=tid)
    assert b.startswith(f"# assay brief · {lead.project_id} · ")
    assert "Goal: Maximise score." in b and "Metric: score (max) · Best: 2.02" in b
    assert f"## Your task t:{str(tid)[:8]}" in b
    assert "## Verified (1)" in b and "oracle: rerun" in b
    assert "## Supported, NOT verified (1)" in b
    assert "## Already tried, do not repeat (2 of 2 relevant)" in b
    assert "failed: overlap violations" in b
    assert "## Open questions (no evidence yet)" in b and "Boundary circles first" in b
    assert "first brief for worker-1" in b
    assert b.rstrip().endswith("assay_record_experiment (failures too).")
    lead.as_agent("worker-3").record_experiment({"init": "hex", "optimizer": "x"}, {"score": 3.0})
    b2 = worker.brief(task_id=tid)
    assert "## Since your last brief" in b2 and "1 new experiments, 1 new best" in b2


def test_budget_trims_bottom_first(lead: Ledger, worker: Ledger) -> None:
    for i in range(60):
        worker.record_experiment({"variant": i, "long_parameter_name": "x" * 40}, {"score": i % 7},
                                 run_status="ok" if i % 3 else "error", notes="some failure reason " * 3)
    for i in range(30):
        worker.propose_claim("hypothesis", f"hypothesis number {i} about something fairly specific")
    full = worker.brief(budget_tokens=20000)
    small = worker.brief(budget_tokens=400)
    assert estimate_tokens(small) <= 400 * 1.05
    assert "more: assay_search" in small
    assert estimate_tokens(full) > estimate_tokens(small)
    assert "Details: assay_get" in small and "Goal:" in small


def test_long_header_and_small_budget_fit_or_reject(db_url: str) -> None:
    project = "brief-long-metadata"
    Ledger.init_project(project, goal="가나다" * 1500, metric="score", direction="max", database_url=db_url)
    lead = Ledger.open(project=project, agent="lead", database_url=db_url)
    best = lead.record_experiment({"method": "test"}, {"score": 1})
    [task] = lead.create_tasks([{"title": "실험" * 1000, "tags": ["긴태그" * 1000], "spec": {"long": "가" * 500}}])
    brief = lead.brief(task, budget_tokens=160)
    assert estimate_tokens(brief) <= 168
    assert "Goal: 가나다" in brief and "Metric: score (max)" in brief
    assert best.id is not None and short("experiment", best.id) in brief
    assert f"## Your task {short('task', task)}" in brief and '"실험' in brief
    assert "Tags: 긴태그" in brief and "Spec: {" in brief
    assert "…" in brief and "Details: assay_get" in brief
    with pytest.raises(ValidationFailed, match="budget_tokens: too small"):
        lead.brief(task, budget_tokens=30)


def test_recent_brief_excludes_invalidated_experiments(lead: Ledger, worker: Ledger) -> None:
    lead.brief()
    dropped = worker.record_experiment({"method": "broken"}, {"score": 99})
    valid = worker.record_experiment({"method": "valid"}, {"score": 2})
    assert dropped.id is not None and valid.id is not None
    lead.decide(dropped.id, "invalidated", "human", "Broken evaluator")
    brief = lead.brief()
    assert "1 new experiments" in brief
    assert short("experiment", dropped.id) not in brief
    assert short("experiment", valid.id) in brief


def _rand_ops() -> st.SearchStrategy:
    op = st.one_of(
        st.tuples(st.just("exp"), st.integers(0, 6), st.integers(0, 3), st.sampled_from(["ok", "error"]),
                  st.floats(0, 10, allow_nan=False)),
        st.tuples(st.just("claim"), st.integers(0, 50)),
        st.tuples(st.just("decide"), st.sampled_from(["supported", "refuted", "verified"])),
        st.tuples(st.just("invalidate"),),
        st.tuples(st.just("task"), st.sampled_from(["a", "b"])),
    )
    return st.lists(op, max_size=25)


@settings(max_examples=int(os.environ.get("ASSAY_PROPERTY_EXAMPLES", "1000")), deadline=None,
          suppress_health_check=list(HealthCheck))
@given(ops=_rand_ops(), budget=st.integers(300, 3000), use_task=st.booleans())
def test_i5_property(db_url: str, ops: list, budget: int, use_task: bool) -> None:
    pid = make_project(db_url)
    lead = Ledger.open(project=pid, agent="lead", database_url=db_url)
    w = Ledger.open(project=pid, agent="w", database_url=db_url)
    exps: list = []
    claims: list = []
    tasks: list = []
    for o in ops:
        if o[0] == "exp":
            r = w.record_experiment({"k": o[1], "pad": "y" * 30}, {"score": o[4]}, seed=o[2], run_status=o[3],
                                    task_id=tasks[-1] if tasks else None)
            if r.id:
                exps.append(r.id)
        elif o[0] == "claim":
            claims.append(w.propose_claim("hypothesis", f"claim {o[1]}",
                                          evidence=[(exps[-1], "supports")] if exps else None).id)
        elif o[0] == "decide" and claims:
            ev = exps[-1:] or []
            if o[1] == "verified" and not ev:
                continue
            lead.decide(claims[-1], o[1], "metric", "r", ev, force=True)
        elif o[0] == "invalidate" and exps:
            lead.decide(exps[0], "invalidated", "human", "bug")
        elif o[0] == "task":
            tasks += lead.create_tasks([{"title": f"task {o[1]}", "tags": [o[1]],
                                         "spec": {"config_space": {"k": [1, 2]}}}])
    tid = tasks[-1] if (use_task and tasks) else None
    b1 = compile_brief(w, tid, budget, now=NOW, log=False)
    b2 = compile_brief(w, tid, budget, now=NOW, log=False)
    assert b1 == b2
    assert estimate_tokens(b1) <= budget * 1.05
    for pfx, body in ID_RE.findall(b1):
        lead.resolve(f"{pfx}:{body}")  # raises if missing
