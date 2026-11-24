"""[verify] parallel_baseline (B2 step 2a): the merge-base's JUnit run for the test counts goes beside the commit's.
Same verdicts (tests/test_mutations.py runs its table both ways); here: it overlaps, it stops with the commit's run,
and the setting is read strictly."""
from __future__ import annotations

import signal
import tempfile
import time
from pathlib import Path

import pytest

from benchbook import Ledger, checks
from benchbook.config import VerifyPrep, parse_verify
from benchbook.errors import BenchbookError
from benchbook.runner import Interrupted
from tests.conftest import PY, git, make_project, make_repo, piece

SLOW = f"""project = "ignored"

[checks.unit]
cmd = "{PY} -m pytest -q -p no:cacheprovider tests --junitxml={{junit}}"
junit = true
timeout = 120

[guard]
paths = ["tests/**"]
base = "main"
"""
SLEEPY = "import os\nimport time\n\n\ndef test_slow():\n    time.sleep(float(os.environ.get('SLEEP_S', '0')))\n"


@pytest.fixture
def lead(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    d = tmp_path / "vtmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    pid = make_project(db_url, metric="checks_passed", direction="max", pack="code",
                       settings={"partition_key": "piece"})
    return Ledger.open(project=pid, agent="lead", role="orchestrator", database_url=db_url)


def _repo(root: Path, parallel: bool) -> tuple[Path, str]:
    repo = make_repo(root, SLOW + ("\n[verify]\nparallel_baseline = true\n" if parallel else ""))
    (repo / "tests" / "test_slow.py").write_text(SLEEPY)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "slow suite")
    return repo, piece(repo, "more-tests", {"impl.txt": "fixed\n",
                                            "tests/test_more.py": "def test_more():\n    assert True\n"})


def test_the_merge_base_run_overlaps_the_commits(tmp_path: Path, lead: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLEEP_S", "1.5")
    took = {}
    for parallel in (False, True):
        repo, sha = _repo(tmp_path / f"repo-{parallel}", parallel)
        t = time.monotonic()
        v, rec = checks.verify(lead, "more-tests", sha, repo=repo, reported_by="impl")
        took[parallel] = time.monotonic() - t
        assert (v.integrity.tests_before, v.integrity.tests_after) == (3, 4)
        assert v.all_passed and not v.integrity.guard_weakened
        assert rec.outcome is not None and rec.outcome.decision == "verified", rec.outcome.reason
    assert took[True] < took[False] - 1.0, took


def test_an_interrupted_commit_run_stops_the_merge_base_run(tmp_path: Path, lead: Ledger,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM during the commit's checks: the merge-base's check (here 30 s long) is killed, its worktree removed,
    and verify raises the interruption at once."""
    monkeypatch.setenv("SLEEP_S", "30")
    repo, sha = _repo(tmp_path / "repo", True)
    real = checks.run_checks
    started = time.monotonic()

    def commit_interrupted(repo_: Path, commit: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if commit == sha:
            time.sleep(1.0)  # the merge-base run is in its check by now
            return [checks.CheckResult("unit", "not_run")], Interrupted(signal.SIGTERM)
        return real(repo_, commit, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(checks, "run_checks", commit_interrupted)
    with pytest.raises(Interrupted):
        checks.verify(lead, "more-tests", sha, repo=repo, reported_by="impl")
    assert time.monotonic() - started < 15
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_parallel_baseline_is_read_strictly() -> None:
    assert parse_verify({"parallel_baseline": True}).parallel_baseline
    assert parse_verify({"parallel_baseline": True}).table() == {"parallel_baseline": True}
    assert VerifyPrep().table() == {}  # pins without it are unchanged
    with pytest.raises(BenchbookError, match="parallel_baseline must be true or false"):
        parse_verify({"parallel_baseline": "yes"})
