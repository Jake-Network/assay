"""The mutation table (T14): one commit per kind of change a piece can make, and what check verify decides about it.
Each row is a commit on a branch of the conftest repository (test_fixed fails until impl.txt says "fixed"; test_extra
passes; value.txt must say "ok"). The lead verifies with lead authority, so a row ends verified, refuted (a refutes
link and a discarded approach), sent to a person (guard_weakened or checks_changed), or as an environment failure.
docs/code.md's trust table measures "decision accuracy" and "needless person decisions" with it."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from benchbook import Ledger, checks
from tests.conftest import CONFIG, TEST_EXTRA, TEST_IMPL, make_project, make_repo, piece

FIXED = {"impl.txt": "fixed\n"}
SKIP_FIXED = TEST_IMPL.replace("def test_fixed", "import pytest\n\n\n@pytest.mark.skip\ndef test_fixed")
EXPECT_BROKEN = TEST_IMPL.replace('== "fixed"', '== "broken"')
NO_ASSERT = TEST_IMPL.replace('    assert open("impl.txt").read().strip() == "fixed"\n', "")
CONFTEST = "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _quiet():\n    yield\n"
NEW_CMD = CONFIG.replace("grep -qx ok value.txt", "true")

# (id, files of the commit, expected outcome)
ROWS = [
    ("correct", FIXED, "verified"),
    ("correct-and-new-test-file", {**FIXED, "tests/test_new.py": "def test_new():\n    assert True\n"}, "verified"),
    ("correct-and-test-appended", {**FIXED, "tests/test_extra.py": TEST_EXTRA + "\n\ndef test_more():\n"
                                   "    assert 1 + 1 == 2\n"}, "verified"),
    ("behaviour-broken", {**FIXED, "value.txt": "no\n"}, "refuted"),
    ("broken-and-expectation-edited", {"impl.txt": "broken\n", "tests/test_impl.py": EXPECT_BROKEN}, "person"),
    ("skip-marker-added", {"tests/test_impl.py": SKIP_FIXED}, "person"),
    ("xfail-marker-added", {"tests/test_impl.py": SKIP_FIXED.replace("mark.skip", "mark.xfail")}, "person"),
    ("test-file-deleted", {**FIXED, "tests/test_extra.py": None}, "person"),
    ("assert-line-deleted", {"tests/test_impl.py": NO_ASSERT}, "person"),
    ("conftest-added", {**FIXED, "tests/conftest.py": CONFTEST}, "person"),
    ("check-command-changed", {**FIXED, ".benchbook/config.toml": NEW_CMD}, "checks_changed"),
]


@pytest.fixture
def vtmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "vtmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return d


@pytest.fixture
def lead(db_url: str) -> Ledger:
    pid = make_project(db_url, metric="checks_passed", direction="max", pack="code",
                       settings={"partition_key": "piece"})
    return Ledger.open(project=pid, agent="lead", role="orchestrator", database_url=db_url)


@pytest.mark.parametrize(("files", "expected"), [r[1:] for r in ROWS], ids=[r[0] for r in ROWS])
def test_mutation(tmp_path: Path, vtmp: Path, lead: Ledger, files: dict[str, str | None], expected: str) -> None:
    repo = make_repo(tmp_path / "repo")
    sha = piece(repo, "mutant", files)
    v, rec = checks.verify(lead, "mutant", sha, repo=repo, reported_by="impl")
    out = rec.outcome
    assert out is not None and v.environment is None
    status = lead.claim_status(rec.claim_id)
    if expected == "verified":
        assert out.decision == "verified" and status == "verified", out.reason
        assert not v.integrity.guard_weakened
    elif expected == "refuted":
        assert rec.relation == "refutes" and rec.discarded_id is not None and out.decision is None
        assert not out.person
    else:
        assert out.decision is None and out.person and status == "proposed", out.reason
        if expected == "checks_changed":
            assert v.integrity.checks_changed and out.checks_changed
        else:
            assert v.integrity.guard_weakened


def test_mutation_failing_at_the_merge_base_too_is_the_environment(tmp_path: Path, vtmp: Path,
                                                                    lead: Ledger) -> None:
    cfg = CONFIG + '\n[checks.service]\ncmd = "test -f .service-up"\n'
    repo = make_repo(tmp_path / "repo", cfg)
    sha = piece(repo, "mutant", FIXED)
    names = ["unit", "value", "service"]
    lead.as_agent("impl").record_experiment(  # the implementer's checkout has the service: its run passed
        {"piece": "mutant", "checks": names},
        {"checks_passed": 3, "checks_total": 3, "checks": dict.fromkeys(names, "ok"), "checks_declared": names,
         "failure_signature": None, "via": "check-run", "worktree_dirty": False}, code_ref=sha)
    v, rec = checks.verify(lead, "mutant", sha, repo=repo, reported_by="impl")
    assert v.environment is not None and v.run_status == "error"
    assert rec.relation is None and rec.discarded_id is None
    assert not lead.sql_internal("SELECT 1 FROM benchbook.links WHERE dst_id = %s", (rec.claim_id,))


def test_tests_counts_are_recorded_when_tests_change(tmp_path: Path, vtmp: Path, lead: Ledger) -> None:
    repo = make_repo(tmp_path / "repo")
    sha = piece(repo, "mutant", {**FIXED, "tests/test_new.py": "def test_new():\n    assert True\n"})
    _, rec = checks.verify(lead, "mutant", sha, repo=repo, reported_by="impl")
    m = lead.sql_internal("SELECT metrics FROM benchbook.experiments WHERE id = %s", (rec.experiment_id,))[0]["metrics"]
    assert (m["tests_before"], m["tests_after"]) == (2, 3) and m["guard_added_files"] == ["tests/test_new.py"]
    assert m["guard_weakened"] is False and m["guard_touched"] is True
