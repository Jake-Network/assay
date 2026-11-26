"""Verified completion and check integrity (PR 2-P0a): the code pack's [verify] rule all_pass in decide(), and the
decision `benchbook check verify` makes with lead authority (docs/code.md, "Verified completion")."""
from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger
from benchbook.checks import verify as checks
from benchbook.errors import BenchbookError, InvalidTransition
from benchbook.packs import VerifyPolicy, load_pack, read_verify
from tests.conftest import make_project, make_repo, piece
from tests.writer import as_writer, issue

SHA = "a" * 40
PASS = {"checks_passed": 2, "checks_total": 2, "checks": {"unit": "ok", "value": "ok"},
        "checks_declared": ["unit", "value"], "failure_signature": None,
        "guard_touched": False, "guard_files": [], "checks_changed": False}
FAIL = {**PASS, "checks_passed": 1, "checks": {"unit": "fail", "value": "ok"}, "failure_signature": "unit:x"}


@pytest.fixture
def vtmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "vtmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def code_project(db_url: str) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece"})


def open_as(db_url: str, project: str, agent: str, role: str | None = None) -> Ledger:
    return Ledger.open(project=project, agent=agent, role=role, database_url=db_url)


def attempt(lg: Ledger, metrics: dict[str, Any], sha: str = SHA, name: str = "p", seed: int | None = None) -> Any:
    r = lg.record_experiment({"piece": name, "checks": ["unit", "value"]}, metrics, code_ref=sha, seed=seed)
    assert r.id is not None
    return r.id


def completion(lg: Ledger, sha: str = SHA, name: str = "p", **payload: Any) -> Any:
    return lg.propose_claim("completion", f"{name} is complete at {sha[:12]}",
                            payload={"piece": name, "commit": sha, **payload},
                            tags=[f"piece:{name}", f"commit:{sha}"]).id


def decisions(lg: Ledger, claim: Any) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT * FROM benchbook.decisions WHERE target_id = %s ORDER BY created_at", (claim,))


def to_supported(lead: Ledger, claim: Any, evidence: list[Any]) -> None:
    lead.decide(claim, "supported", "rerun", "re-run passed", evidence)


# ------------------------------------------------------------------------------------------- the pack's policy

def test_code_pack_declares_all_pass_and_the_others_keep_replication() -> None:
    assert load_pack("code").verify == VerifyPolicy("all_pass", 1, 2)
    assert load_pack("ml-research").verify == VerifyPolicy() == load_pack("core").verify
    assert VerifyPolicy().rule == "replication"


@pytest.mark.parametrize(("table", "message"), [
    ({"rule": "majority"}, "rule must be one of"),
    ({"rule": "all_pass", "min_author": 2}, "unknown key min_author"),
    ({"rule": "all_pass", "min_authors": 1}, "min_authors must be an integer >= 2"),
    ({"rule": "all_pass", "min_runs": 0}, "min_runs must be an integer >= 1"),
    ({"rule": "all_pass", "min_runs": True}, "min_runs must be"),
    ({"min_runs": 2}, "rerun_min_seeds"),  # replication's thresholds are the project settings
    ("all_pass", "must be a table"),
])
def test_bad_verify_tables(table: Any, message: str) -> None:
    with pytest.raises(BenchbookError, match=message):
        read_verify("x", table)


def test_replication_rule_is_unchanged_for_other_packs(db_url: str, project: str) -> None:
    """A core project's rerun oracle still asks for seeds and authors (tests/test_referee.py has the rest)."""
    worker = open_as(db_url, project, "w1")
    lead = open_as(db_url, project, "lead")
    e = worker.record_experiment({"a": 1}, {"score": 1.0}, seed=1).id
    c = worker.propose_claim("observation", "a=1 scores 1", evidence=[(e, "supports")]).id
    to_supported(lead, c, [e])
    with pytest.raises(InvalidTransition, match="distinct seeds"):
        lead.decide(c, "verified", "rerun", "x", [e])


# ------------------------------------------------------------------------------------------- the rule in decide()

@pytest.mark.parametrize("statuses", [{"unit": "fail", "value": "ok"}, {"unit": "ok"}])
def test_a_rerun_requires_consistent_check_results(db_url: str, code_project: str,
                                                 statuses: dict[str, str]) -> None:
    impl = open_as(db_url, code_project, "impl")
    lead = open_as(db_url, code_project, "lead")
    claim = completion(impl)
    inconsistent = attempt(lead, {**PASS, "checks": statuses})
    with pytest.raises(InvalidTransition, match="not every attempt"):
        lead.check_rerun(claim, [inconsistent])


def test_a_self_recorded_pass_alone_cannot_verify(db_url: str, code_project: str) -> None:
    impl = open_as(db_url, code_project, "impl")
    lead = open_as(db_url, code_project, "lead")
    own = attempt(impl, PASS)
    claim = completion(impl)
    to_supported(lead, claim, [own])
    with pytest.raises(InvalidTransition, match=r"other than the implementer \(impl\).*oracle human"):
        lead.decide(claim, "verified", "rerun", "the implementer says so", [own])
    with pytest.raises(InvalidTransition, match="oracle human"):
        lead.check_rerun(claim, [own])  # the dry run says the same and writes nothing
    assert lead.claim_status(claim) == "supported"
    # one independent passing re-run is enough (min_runs 1, min_authors 2), with or without the own pass cited
    rerun = attempt(open_as(db_url, code_project, "verifier", "harness"), PASS)
    lead.decide(claim, "verified", "rerun", "independent re-run passed", [rerun, own])
    check = decisions(lead, claim)[-1]["oracle_detail"]["rerun_check"]
    assert check == {"rule": "all_pass", "piece": "p", "commit": SHA, "implementers": ["impl"], "runs": 1,
                     "authors": ["verifier"], "attempts": 2}


def test_runs_from_the_implementers_checkout_are_not_independent(db_url: str, code_project: str) -> None:
    """The Bash hook records an implementer's run as `hook`, and check run may run under any name: a different
    author is not a different checkout. A run with uncommitted changes did not test the commit at all."""
    impl = open_as(db_url, code_project, "impl")
    lead = open_as(db_url, code_project, "lead")
    hook = open_as(db_url, code_project, "hook", "harness")
    claim = completion(impl)
    hooked = attempt(hook, {**PASS, "via": "bash-hook", "worktree_dirty": False}, seed=1)
    wrapped = attempt(open_as(db_url, code_project, "someone"), {**PASS, "via": "check-run"}, seed=2)
    to_supported(lead, claim, [hooked])
    with pytest.raises(InvalidTransition, match=r"recorded from its checkout.*oracle human"):
        lead.decide(claim, "verified", "rerun", "the hook saw it pass", [hooked, wrapped])
    # a dirty run is no attempt of the commit: it cannot be cited, and its failure does not block the commit
    dirty = attempt(hook, {**PASS, "checks_passed": 0, "via": "bash-hook", "worktree_dirty": True}, seed=3)
    with pytest.raises(InvalidTransition, match="must be valid finished attempts"):
        lead.check_rerun(claim, [dirty])
    attempts = lead.completion_attempts(claim)
    assert attempts is not None and dirty not in [a["id"] for a in attempts["attempts"]]
    rerun = attempt(open_as(db_url, code_project, "verifier", "harness"), PASS, seed=4)
    lead.decide(claim, "verified", "rerun", "independent re-run passed", [rerun, hooked])
    check = decisions(lead, claim)[-1]["oracle_detail"]["rerun_check"]
    assert (check["runs"], check["authors"], check["attempts"]) == (1, ["verifier"], 3)
    # a clean failing run from the implementer's checkout at the same commit still disagrees
    other = completion(impl, sha="b" * 40)
    attempt(hook, {**PASS, "checks_passed": 0, "via": "bash-hook"}, sha="b" * 40, seed=5)
    passing = attempt(open_as(db_url, code_project, "verifier", "harness"), PASS, sha="b" * 40, seed=6)
    to_supported(lead, other, [passing])
    with pytest.raises(InvalidTransition, match="not every attempt"):
        lead.decide(other, "verified", "rerun", "re-run passed", [passing])


def test_the_implementer_cannot_verify_its_own_completion_by_any_route(db_url: str, code_project: str) -> None:
    lead = open_as(db_url, code_project, "lead")
    rerun = attempt(open_as(db_url, code_project, "verifier", "harness"), PASS)
    # the lead reports a piece itself: an independent re-run exists, but the lead may not decide its own completion
    mine = completion(lead)
    to_supported(lead, mine, [rerun])
    for oracle in ("rerun", "human"):
        with pytest.raises(InvalidTransition, match="cannot verify its own completion"):
            lead.decide(mine, "verified", oracle, "mine", [rerun])
    open_as(db_url, code_project, "human").decide(mine, "verified", "human", "read it", [rerun])  # another person may

    # a worker that names someone else as the reporter stays an implementer: its own pass still does not count
    impl = open_as(db_url, code_project, "impl")
    other = "b" * 40
    own = attempt(impl, PASS, other)
    claim = completion(impl, other, reported_by="ghost")
    to_supported(lead, claim, [own])
    with pytest.raises(InvalidTransition, match=r"implementer \(ghost, impl\)"):
        lead.decide(claim, "verified", "rerun", "x", [own])
    # only a lead, a person or a harness verifier records a claim for someone else (check verify under a token)
    third = "c" * 40
    on_behalf = completion(lead, third, reported_by="impl")
    own3 = attempt(impl, PASS, third)
    lead_run = attempt(lead, PASS, third)
    to_supported(lead, on_behalf, [lead_run])
    with pytest.raises(InvalidTransition, match=r"implementer \(impl\)"):
        lead.decide(on_behalf, "verified", "rerun", "x", [own3])
    assert lead.decide(on_behalf, "verified", "rerun", "x", [lead_run]).new_status == "verified"


def test_guard_or_checks_changes_block_rerun_but_not_a_person(db_url: str, code_project: str) -> None:
    lead = open_as(db_url, code_project, "lead")
    v1, v2 = open_as(db_url, code_project, "v1", "harness"), open_as(db_url, code_project, "v2", "harness")
    for sha, flags, match in (("d" * 40, {"guard_touched": True, "guard_files": ["tests/test_x.py"]},
                               r"changed guarded files in a way that can weaken them \(tests/test_x.py\)"),
                              ("e" * 40, {"checks_changed": True}, r"changed \[checks\]")):
        claim = completion(open_as(db_url, code_project, "impl"), sha)
        flagged = attempt(v1, {**PASS, **flags}, sha)
        clean = attempt(v2, PASS, sha)  # a clean re-run of the same commit does not wash the flag out
        to_supported(lead, claim, [clean])
        for evidence in ([clean], [flagged], [clean, flagged]):
            with pytest.raises(InvalidTransition, match=f"{match}.*oracle human"):
                lead.decide(claim, "verified", "rerun", "green", evidence)
        assert open_as(db_url, code_project, "human").decide(
            claim, "verified", "human", "the test change is fine", [clean, flagged]).oracle == "human"


def test_refuting_evidence_blocks_rerun_verification(db_url: str, code_project: str) -> None:
    """A failing attempt of the same piece and commit, cited or not, by anyone, keeps the rerun oracle out."""
    lead = open_as(db_url, code_project, "lead")
    claim = completion(open_as(db_url, code_project, "impl"))
    failed = attempt(open_as(db_url, code_project, "v1", "harness"), FAIL)
    lead.link(failed, "refutes", claim)
    passed = attempt(open_as(db_url, code_project, "v2", "harness"), PASS)
    to_supported(lead, claim, [passed])
    with pytest.raises(InvalidTransition, match=r"not every attempt of p at aaaaaaaaaaaa passed.*by v1 failed"):
        lead.decide(claim, "verified", "rerun", "x", [passed])
    # once a lead invalidates the failing run (a broken runner, say), the rule accepts the passing one
    lead.decide(failed, "invalidated", "human", "the runner had no network")
    assert lead.decide(claim, "verified", "rerun", "x", [passed]).new_status == "verified"
    st = lead.sql_internal("SELECT status, n_refute FROM benchbook.v_claim_status WHERE id = %s", (claim,))[0]
    assert st == {"status": "verified", "n_refute": 0}  # invalidated evidence is not counted


def test_evidence_must_be_attempts_of_the_claims_piece_and_commit(db_url: str, code_project: str) -> None:
    lead = open_as(db_url, code_project, "lead")
    v = open_as(db_url, code_project, "v1", "harness")
    claim = completion(open_as(db_url, code_project, "impl"))
    elsewhere = [attempt(v, PASS, "f" * 40), attempt(v, PASS, SHA, name="other")]
    to_supported(lead, claim, elsewhere)
    for e in elsewhere:
        with pytest.raises(InvalidTransition, match="attempts of p at aaaaaaaaaaaa"):
            lead.decide(claim, "verified", "rerun", "x", [e])
    note = lead.propose_claim("note", "a note")
    to_supported(lead, note.id, elsewhere)
    with pytest.raises(InvalidTransition, match="only completion claims"):
        lead.decide(note.id, "verified", "rerun", "x", elsewhere[:1])


def test_min_runs_and_min_authors_are_honoured(db_url: str, code_project: str) -> None:
    lead = open_as(db_url, code_project, "lead")
    lead.pack = dataclasses.replace(lead.pack, verify=VerifyPolicy("all_pass", min_runs=2, min_authors=3))
    claim = completion(open_as(db_url, code_project, "impl"))
    v1, v2 = open_as(db_url, code_project, "v1", "harness"), open_as(db_url, code_project, "v2", "harness")
    a, b = attempt(v1, PASS), attempt(v1, PASS)  # two runs, one agent besides the implementer
    to_supported(lead, claim, [a])
    with pytest.raises(InvalidTransition, match=r"by >= 2 agent\(s\).*has 2 by 1 \(v1\)"):
        lead.decide(claim, "verified", "rerun", "x", [a, b])
    c = attempt(v2, PASS)
    with pytest.raises(InvalidTransition, match=r">= 2 passing re-run\(s\).*has 1 by 1"):
        lead.decide(claim, "verified", "rerun", "x", [c])
    lead.decide(claim, "verified", "rerun", "x", [a, c])
    assert decisions(lead, claim)[-1]["oracle_detail"]["rerun_check"]["authors"] == ["v1", "v2"]


# ------------------------------------------------------------------------------------------- check verify decides

def test_lead_verify_of_a_clean_passing_commit_verifies(repo: Path, vtmp: Path, db_url: str,
                                                       code_project: str) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    lead = open_as(db_url, code_project, "lead")
    _, rec = checks.verify(lead, "media", sha, repo=repo, reported_by="impl")
    assert rec.outcome is not None and rec.outcome.decision == "verified" and not rec.outcome.person
    assert rec.outcome.evidence == [rec.experiment_id]
    assert lead.claim_status(rec.claim_id) == "verified"
    rows = decisions(lead, rec.claim_id)
    assert [(d["new_status"], d["oracle"], d["decided_by"]) for d in rows] == [
        ("supported", "rerun", "lead"), ("verified", "rerun", "lead")]
    assert [d["id"] for d in rows] == rec.outcome.decision_ids
    assert rows[1]["evidence_ids"] == [rec.experiment_id]
    assert rows[1]["oracle_detail"]["rerun_check"]["implementers"] == ["impl"]
    # verifying the same commit again: a repeat (duplicate_of), the same claim, and no second decision
    _, again = checks.verify(lead, "media", sha, repo=repo, reported_by="impl")
    assert again.duplicate_of == rec.experiment_id and again.claim_id == rec.claim_id
    assert again.outcome is not None and again.outcome.decision is None and "already verified" in again.outcome.reason
    assert len(decisions(lead, rec.claim_id)) == 2


@pytest.mark.parametrize("change", [
    {"impl.txt": "fixed\n", "tests/test_extra.py": None},  # deletes a test
    {"tests/test_impl.py": "def test_fixed():\n    assert True\n"},  # weakens the test instead of fixing impl
])
def test_a_commit_that_deletes_or_weakens_a_test_is_not_verified(repo: Path, vtmp: Path, db_url: str,
                                                                 code_project: str,
                                                                 change: dict[str, str | None]) -> None:
    sha = piece(repo, "sneaky", change)
    lead = open_as(db_url, code_project, "lead")
    v, rec = checks.verify(lead, "media", sha, repo=repo, reported_by="impl")
    assert v.all_passed  # the re-run is green ...
    assert rec.outcome is not None and rec.outcome.decision is None and rec.outcome.person  # ... and decides nothing
    assert rec.outcome.guard_files == sorted(change.keys() - {"impl.txt"})
    assert "a person has to decide" in rec.outcome.reason
    assert lead.claim_status(rec.claim_id) == "proposed" and not decisions(lead, rec.claim_id)
    to_supported(lead, rec.claim_id, [rec.experiment_id])
    with pytest.raises(InvalidTransition, match="guarded files"):  # nor by hand through the rerun oracle
        lead.decide(rec.claim_id, "verified", "rerun", "green", [rec.experiment_id])
    human = open_as(db_url, code_project, "human")
    assert human.decide(rec.claim_id, "verified", "human", "the test was obsolete",
                        [rec.experiment_id]).new_status == "verified"


def test_disagreement_with_the_implementers_own_pass_is_inconclusive(repo: Path, vtmp: Path, db_url: str,
                                                                    code_project: str) -> None:
    sha = piece(repo, "bad", {"impl.txt": "almost\n"})
    own = attempt(open_as(db_url, code_project, "impl"), PASS, sha, name="media")  # "it passed on my machine"
    lead = open_as(db_url, code_project, "lead")
    v, rec = checks.verify(lead, "media", sha, repo=repo, reported_by="impl")
    assert not v.all_passed and rec.outcome is not None and rec.outcome.decision == "inconclusive"
    assert lead.claim_status(rec.claim_id) == "inconclusive"
    [d] = decisions(lead, rec.claim_id)
    assert set(d["evidence_ids"]) == {rec.experiment_id, own} and d["oracle"] == "rerun"
    assert rec.outcome.evidence == [rec.experiment_id, own]


def test_a_failing_rerun_without_an_own_pass_decides_nothing(repo: Path, vtmp: Path, db_url: str,
                                                            code_project: str) -> None:
    sha = piece(repo, "bad", {"impl.txt": "almost\n"})
    lead = open_as(db_url, code_project, "lead")
    _, rec = checks.verify(lead, "media", sha, repo=repo, reported_by="impl")
    assert rec.relation == "refutes" and rec.outcome is not None and rec.outcome.decision is None
    assert lead.claim_status(rec.claim_id) == "proposed"


def test_without_lead_authority_verify_records_and_says_it_did_not_decide(repo: Path, vtmp: Path, db_url: str,
                                                                         code_project: str) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    harness = open_as(db_url, code_project, "verifier@test", "harness")
    _, rec = checks.verify(harness, "media", sha, repo=repo, reported_by="impl")
    assert rec.relation == "supports" and rec.outcome is not None and rec.outcome.decision is None
    assert "without a decision" in rec.outcome.reason and "harness" in rec.outcome.reason
    assert not decisions(harness, rec.claim_id)
    # the lead can decide later from that record: the harness verifier is not the implementer
    lead = open_as(db_url, code_project, "lead")
    to_supported(lead, rec.claim_id, [rec.experiment_id])
    assert lead.decide(rec.claim_id, "verified", "rerun", "x", [rec.experiment_id]).new_status == "verified"


def test_under_a_token_the_verifier_records_the_claim_and_names_the_reporter(repo: Path, vtmp: Path, db_url: str,
                                                                            code_project: str) -> None:
    sha = piece(repo, "good", {"impl.txt": "fixed\n"})
    writer = as_writer(db_url)
    impl = Ledger.open(project=code_project, database_url=writer, token=issue(db_url, code_project, "impl", "worker"))
    own = attempt(impl, PASS, sha, name="media")
    # the implementer cannot report under its name and re-run under another: its token is one name, and not a lead's
    _, mine = checks.verify(impl, "media", sha, repo=repo, reported_by="someone-else")
    assert mine.outcome is not None and mine.outcome.decision is None and "role worker" in mine.outcome.reason
    claim_row = impl.sql_internal("SELECT author, payload FROM benchbook.claims WHERE id = %s", (mine.claim_id,))[0]
    assert claim_row["author"] == "impl" and claim_row["payload"]["reported_by"] == "someone-else"

    lead = Ledger.open(project=code_project, database_url=writer, token=issue(db_url, code_project, "lead"))
    other = piece(repo, "good2", {"impl.txt": "fixed\n", "value.txt": "ok\n", "extra.txt": "x\n"})
    _, rec = checks.verify(lead, "media", other, repo=repo, reported_by="impl")
    row = lead.sql_internal("SELECT author, payload FROM benchbook.claims WHERE id = %s", (rec.claim_id,))[0]
    assert row["author"] == "lead" and row["payload"]["reported_by"] == "impl"
    assert rec.outcome is not None and rec.outcome.decision == "verified"
    detail = decisions(lead, rec.claim_id)[-1]["oracle_detail"]["rerun_check"]
    assert detail["implementers"] == ["impl"] and detail["authors"] == ["lead"]
    # the first claim names impl and someone-else; impl's own runs never verify it, whoever decides
    to_supported(lead, mine.claim_id, [own])
    with pytest.raises(InvalidTransition, match="other than the implementer"):
        lead.decide(mine.claim_id, "verified", "rerun", "x", [own, mine.experiment_id])


# ------------------------------------------------------------------------------------------- CLI

def run_cli(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "benchbook.cli", *args], cwd=cwd, env=env, capture_output=True,
                          text=True, timeout=180)


def test_cli_verify_decides_with_lead_authority(repo: Path, vtmp: Path, db_url: str, code_project: str) -> None:
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url, "TMPDIR": str(vtmp)}
    for k in ("BENCHBOOK_PROJECT", "ASSAY_PROJECT", "BENCHBOOK_AGENT", "ASSAY_AGENT", "BENCHBOOK_TOKEN",
              "ASSAY_TOKEN"):
        env.pop(k, None)
    good = piece(repo, "good", {"impl.txt": "fixed\n"})
    sneaky = piece(repo, "sneaky", {"impl.txt": "fixed\n", "tests/test_extra.py": None})
    base = ["--project", code_project]
    p = run_cli(repo, env, *base, "check", "verify", "media", good, "--reported-by", "impl")
    assert p.returncode == 0, p.stderr
    assert "decision: recorded without a decision" in p.stdout
    assert f"benchbook --agent lead check verify media {good} --claim " in p.stdout  # what to run
    p = run_cli(repo, env, *base, "--agent", "lead", "check", "verify", "media", good, "--reported-by", "impl",
                "--json")
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["decision"] == "verified" and out["needs_person"] is False and len(out["decisions"]) == 2
    assert out["evidence"][0] == out["experiment"] and len(out["evidence"]) == 2  # both re-runs of that commit
    p = run_cli(repo, env, *base, "--agent", "lead", "check", "verify", "media", sneaky, "--reported-by", "impl")
    assert p.returncode == 0, p.stderr  # every check passed ...
    assert "a person has to decide" in p.stdout and "  changed: tests/test_extra.py" in p.stdout  # ... no decision
    claim = Ledger.open(project=code_project, agent="lead", database_url=db_url).sql_internal(
        "SELECT id FROM benchbook.claims WHERE project_id = %s AND %s = ANY(tags)", (code_project, f"commit:{sneaky}"))
    assert Ledger.open(project=code_project, agent="lead", database_url=db_url).claim_status(
        claim[0]["id"]) == "proposed"
    assert uuid.UUID(str(claim[0]["id"]))


def test_a_nameless_rerun_names_what_decides_only_when_it_can(repo: Path, vtmp: Path, db_url: str,
                                                             code_project: str) -> None:
    """After a re-run without lead authority: the lead's re-run (with the same base and project) only when it would
    decide, a person when the lead reported the piece itself, and no command after a failed run."""
    harness = open_as(db_url, code_project, "verifier@test", "harness")
    good = piece(repo, "good", {"impl.txt": "fixed\n"})
    _, rec = checks.verify(harness, "media", good, repo=repo, reported_by="impl")
    assert rec.outcome is not None and f" {good} --claim {rec.claim_id} --base " in rec.outcome.reason
    assert rec.outcome.reason.endswith(f" --project {code_project}")
    _, rec = checks.verify(harness, "own", good, repo=repo, reported_by="lead")
    assert rec.outcome is not None and "A person decides it: benchbook decide " in rec.outcome.reason
    bad = piece(repo, "bad", {"impl.txt": "almost\n"})
    _, rec = checks.verify(harness, "broken", bad, repo=repo, reported_by="impl")
    assert rec.relation == "refutes" and rec.outcome is not None
    assert rec.outcome.reason.endswith("(orchestrator or human role)")  # a lead's re-run would not decide either
    worker = open_as(db_url, code_project, "impl-2")  # a named worker is not handed the lead's command
    _, rec = checks.verify(worker, "peer", good, repo=repo, reported_by="impl")
    assert rec.outcome is not None and rec.outcome.reason.endswith("(orchestrator or human role)")


def test_a_piece_started_from_an_integration_branch_lists_only_its_own_files(repo: Path, vtmp: Path, db_url: str,
                                                                            code_project: str) -> None:
    """Fourth boundary pilot: the worker of one piece branched from an integration branch where another piece was
    merged; the merge-base diff listed the other piece's file too, which a later change then marked changed since."""
    lead = open_as(db_url, code_project, "lead")
    first = piece(repo, "alpha", {"a.py": "a = 1\n"})
    checks.verify(lead, "alpha", first, repo=repo, reported_by="impl-a")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "integration", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--no-ff", "-m", "merge alpha", "alpha"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "beta", "integration"], check=True)
    (repo / "b.py").write_text("b = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "b.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "beta"], check=True)
    second = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                            text=True).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    v, rec = checks.verify(lead, "beta", second, repo=repo, reported_by="impl-b")  # [guard] base: main
    assert v.base.merge_base == subprocess.run(["git", "-C", str(repo), "rev-parse", "main"], check=True,
                                               capture_output=True, text=True).stdout.strip()
    [row] = lead.sql_internal("SELECT payload FROM benchbook.claims WHERE id = %s", (rec.claim_id,))
    assert row["payload"]["files"] == ["b.py"]
    assert v.paths == ["a.py", "b.py"]  # the scope (file: tags) still comes from the merge-base diff


def test_a_piece_keeps_its_own_commits_below_another_pieces_commit(repo: Path, vtmp: Path, db_url: str,
                                                                    code_project: str) -> None:
    """One line mixes two pieces (alpha's a.py, then beta reported at its commit, then alpha's a2.py): only beta's
    commit is left out of alpha's files, not alpha's first commit below it, or a later change to a.py would not mark
    alpha changed since (review of the first version)."""
    lead = open_as(db_url, code_project, "lead")

    def commit(path: str) -> str:
        (repo / path).write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", path], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", path], check=True)
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True).stdout.strip()

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "mixed", "main"], check=True)
    commit("a.py")
    b1 = commit("b.py")
    checks.verify(lead, "beta", b1, repo=repo, reported_by="impl-b")
    a2 = commit("a2.py")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    _, rec = checks.verify(lead, "alpha", a2, repo=repo, reported_by="impl-a")
    [row] = lead.sql_internal("SELECT payload FROM benchbook.claims WHERE id = %s", (rec.claim_id,))
    assert sorted(row["payload"]["files"]) == ["a.py", "a2.py"]


@pytest.mark.parametrize("case", ["merge adjusts a file", "file name with newlines"])
def test_a_piece_lists_what_its_merges_change_and_names_as_git_has_them(repo: Path, vtmp: Path, db_url: str,
                                                                         code_project: str, case: str) -> None:
    """Review of #22: a file the piece's merge commit changed itself (beta's shared.txt, adjusted while merging beta
    in) is the piece's too, or a later change to it would leave the verification marked current; and a path is kept
    byte for byte, newlines included, as the brief's comparison reads it."""
    lead = open_as(db_url, code_project, "lead")

    def git_(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    if case == "merge adjusts a file":
        beta = piece(repo, "beta", {"shared.txt": "beta\n"})
        checks.verify(lead, "beta", beta, repo=repo, reported_by="impl-b")
        git_("checkout", "-q", "-b", "alpha", "main")
        (repo / "a.py").write_text("a = 1\n")
        git_("add", "a.py")
        git_("commit", "-q", "-m", "alpha")
        git_("merge", "-q", "--no-ff", "--no-commit", "beta")
        (repo / "shared.txt").write_text("adjusted\n")
        git_("add", "shared.txt")
        git_("commit", "-q", "-m", "merge beta, adjusting shared.txt")
        want = ["a.py", "shared.txt"]
    else:
        git_("checkout", "-q", "-b", "alpha", "main")
        (repo / "\ntrailing.txt\n").write_text("x\n")
        git_("add", "-A")
        git_("commit", "-q", "-m", "an odd name")
        want = ["\ntrailing.txt\n"]
    head = git_("rev-parse", "HEAD")
    git_("checkout", "-q", "main")
    _, rec = checks.verify(lead, "alpha", head, repo=repo, reported_by="impl-a")
    [row] = lead.sql_internal("SELECT payload FROM benchbook.claims WHERE id = %s", (rec.claim_id,))
    assert sorted(row["payload"]["files"]) == want


def test_the_lead_command_decides_the_claim_the_nameless_run_was_for(repo: Path, vtmp: Path, db_url: str,
                                                                     code_project: str) -> None:
    """Review of #18: two completions by one reporter for the same piece and commit; the nameless run was for the
    second (--claim), and the command it prints, run as printed, must decide that one and not the first."""
    good = piece(repo, "good", {"impl.txt": "fixed\n"})
    impl = open_as(db_url, code_project, "impl")
    first = completion(impl, good, "media")
    second = impl.propose_claim("completion", "media is done, second report", tags=["piece:media", f"commit:{good}"],
                                payload={"piece": "media", "commit": good}).id
    assert first != second
    harness = open_as(db_url, code_project, "verifier@test", "harness")
    _, rec = checks.verify(harness, "media", good, repo=repo, claim=str(second))
    assert rec.outcome is not None and rec.claim_id == second
    hint = rec.outcome.reason.split("run it as the lead: ", 1)[1]
    assert f" {good} --claim {second} --base " in hint  # the whole sha, the claim's id, a fixed base
    env = {**{k: v for k, v in os.environ.items() if not k.startswith(("BENCHBOOK_", "ASSAY_"))},
           "BENCHBOOK_DATABASE_URL": db_url, "TMPDIR": str(vtmp)}
    p = subprocess.run([sys.executable, "-m", "benchbook.cli", *shlex.split(hint)[1:]], cwd=repo, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0 and "decision: verified" in p.stdout, p.stdout + p.stderr
    lead = open_as(db_url, code_project, "lead")
    assert (lead.claim_status(second), lead.claim_status(first)) == ("verified", "proposed")


def test_cli_verify_under_a_lead_token(repo: Path, vtmp: Path, db_url: str, code_project: str) -> None:
    """With BENCHBOOK_TOKEN and no --agent, the token names the verifier (no verifier@<host> to contradict it)."""
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": as_writer(db_url), "TMPDIR": str(vtmp),
           "BENCHBOOK_TOKEN": issue(db_url, code_project, "lead")}
    for k in ("BENCHBOOK_PROJECT", "ASSAY_PROJECT", "BENCHBOOK_AGENT", "ASSAY_AGENT"):
        env.pop(k, None)
    good = piece(repo, "good", {"impl.txt": "fixed\n"})
    p = run_cli(repo, env, "--project", code_project, "check", "verify", "media", good, "--reported-by", "impl",
                "--json")
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["verifier"] == "lead" and out["reported_by"] == "impl" and out["decision"] == "verified"
