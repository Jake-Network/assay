"""The mutation table (T14): one commit per kind of change a piece can make, and what check verify decides about it.
Each row is a commit on a branch of the conftest repository (test_fixed fails until impl.txt says "fixed"; test_extra
passes; value.txt must say "ok"). The lead verifies with lead authority, so a row ends verified, refuted (a refutes
link and a discarded approach), sent to a person (guard_weakened or checks_changed), or as an environment failure.
docs/code.md ("The verification worktree") cites it: every row comes out the same with [verify] parallel_baseline."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from benchbook import Ledger, config
from benchbook.checks import gitrepo, guard
from benchbook.checks import verify as checks
from tests.conftest import CONFIG, PY, TEST_EXTRA, TEST_IMPL, git, make_project, make_repo, piece

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


# [verify] parallel_baseline runs the merge-base's JUnit checks beside the commit's: every row must come out the same.
PARALLEL = "\n[verify]\nparallel_baseline = true\n"


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel-baseline"])
@pytest.mark.parametrize(("files", "expected"), [r[1:] for r in ROWS], ids=[r[0] for r in ROWS])
def test_mutation(tmp_path: Path, vtmp: Path, lead: Ledger, files: dict[str, str | None], expected: str,
                  parallel: bool) -> None:
    repo = make_repo(tmp_path / "repo", CONFIG + (PARALLEL if parallel else ""))
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


# Rows for a test file with imports (after the boundary pilot, where a worker's `get_path)` -> `get_path,` +
# `run_length)` was a weakening): names added to an import are lines added; the rest still goes to a person.
TEST_NAMES = """import os, sys
from os.path import (basename,
                     dirname)
from os.path import join


def test_names():
    assert basename("a/b") == "b"
    assert len([1, 2]) == 2
    assert sorted([3, 1]) == sorted(
        [1, 3])
"""
JS_NAMES = "import { a } from './names.js';\n\ntest('a', () => expect(a).toBe(1));\n"


def _names(old: str, new: str) -> dict[str, str | None]:
    assert old in TEST_NAMES
    return {**FIXED, "tests/test_names.py": TEST_NAMES.replace(old, new)}


IMPORT_ROWS = [
    ("parenthesized-import-extended", _names("dirname)", "dirname,\n                     splitext)"), "verified"),
    ("from-import-extended", _names("import join\n", "import join, normpath\n"), "verified"),
    ("import-extended", _names("import os, sys", "import os, sys, json"), "verified"),
    ("import-line-added", _names("import join\n", "import join\nfrom os.path import normpath\n"), "verified"),
    ("import-name-replaced", _names("import join\n", "import normpath\n"), "person"),
    ("import-name-aliased", _names("import join\n", "import join as j\n"), "person"),
    ("import-module-changed", _names("from os.path import join", "from posixpath import join"), "person"),
    ("import-extended-and-expectation-changed",
     {**_names("import join\n", "import join, normpath\n"),
      "tests/test_names.py": TEST_NAMES.replace("import join\n", "import join, normpath\n")
      .replace("len([1, 2]) == 2", "len([1, 2, 3]) == 3")}, "person"),
    ("import-extended-and-skip-added",
     _names("import join\n\n\ndef", "import join\nimport pytest\n\n\n@pytest.mark.skip\ndef"), "person"),
    # whitespace and a trailing ")" do not make a rewritten line: this adds an argument to a call in an assert
    ("assert-call-argument-added", _names("        [1, 3])", "        [1, 3],\n        reverse=False)"), "person"),
    ("js-import-extended", {**FIXED, "tests/names.test.js": JS_NAMES.replace("{ a }", "{ a, b }")}, "person"),
]


@pytest.mark.parametrize(("files", "expected"), [r[1:] for r in IMPORT_ROWS], ids=[r[0] for r in IMPORT_ROWS])
def test_mutation_of_imports(tmp_path: Path, vtmp: Path, lead: Ledger, files: dict[str, str | None],
                             expected: str) -> None:
    repo = make_repo(tmp_path / "repo", CONFIG)
    (repo / "tests/test_names.py").write_text(TEST_NAMES)
    (repo / "tests/names.test.js").write_text(JS_NAMES)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "names")
    sha = piece(repo, "mutant", files)
    v, rec = checks.verify(lead, "mutant", sha, repo=repo, reported_by="impl")
    out = rec.outcome
    assert out is not None and v.environment is None and v.all_passed
    if expected == "verified":
        assert out.decision == "verified" and lead.claim_status(rec.claim_id) == "verified", out.reason
        assert v.integrity.guard_appended and not v.integrity.guard_weakened
    else:
        assert out.decision is None and out.person, out.reason
        assert v.integrity.guard_weakened_files


@pytest.mark.parametrize("updated", [
    "from os.path import (basename, dirname)\nfrom posixpath import splitext as basename\n\n",
    "from os.path import (basename, dirname)\nfrom os.path import splitext as len\n\n",
    "from os.path import (dirname, basename)\n\n",
    "if True:\n    from os.path import (basename, dirname)\n\n",
], ids=["shadow-import", "shadow-builtin", "reorder", "move-to-other-scope"])
def test_import_rewrite_preserves_the_original_test_bindings(tmp_path: Path, updated: str) -> None:
    repo = make_repo(tmp_path / "repo")
    path = "tests/test_bindings.py"
    test = "def test_existing():\n    assert len(basename('a/b')) == 1\n"
    (repo / path).write_text("from os.path import basename, dirname\n\n" + test)
    git(repo, "add", path)
    git(repo, "commit", "-qm", "original tests")
    base = git(repo, "rev-parse", "HEAD")
    sha = piece(repo, "changed-import", {path: updated + test})
    assert guard.classify_guard(repo, base, sha, [path]) == ([], [], [path])


PILOT = Path(__file__).parent / "fixtures" / "boundary_pilot"
# The pilot's base files, cut down to the context lines of its diffs (the rest of boltons is not needed to apply them)
PILOT_BASE = {
    "tests/test_iterutils.py": """import os

import pytest

from boltons.iterutils import (first,
                               research,
                               default_enter,
                               default_exit,
                               get_path)
from boltons.namedutils import namedtuple

CUR_PATH = os.path.abspath(__file__)


def test_frange_xfrange_zero_step():
    with pytest.raises(ValueError):
        frange(10, step=0)
    with pytest.raises(ValueError):
        list(xfrange(10, step=0))
""",
    "tests/test_mathutils.py": """from pytest import raises
from boltons.mathutils import clamp, ceil, floor, Bits
import math

INF, NAN = float('inf'), float('nan')


def test_bits_rshift_past_width():
    shifted = Bits('1') >> 1
    assert len(shifted) == 0
    assert shifted.as_bin() == ''
    assert Bits('101') >> 1 == Bits('10')
""",
    "tests/test_strutils.py": """from boltons import strutils


def test_args2cmd_sep():
    assert strutils.args2cmd(['aa', 'bb']) == 'aa bb'
    assert strutils.args2cmd(['aa', 'bb'], sep='|') == 'aa|bb'
    assert strutils.args2cmd(['a a', 'bb'], sep='|') == '"a a"|bb'
""",
}


@pytest.mark.parametrize("commit", ["07ee866", "e16c45f", "6feff72"])
def test_pilot_import_rewrites_are_appended(tmp_path: Path, commit: str) -> None:
    """The three pieces of the boundary pilot's arm F that a re-run passed and the guard sent to a person: each added
    tests and one name to an import (`get_path)` -> `get_path,` + `run_length)`; `..., Bits` -> `..., Bits,
    weighted_median`). None of them is a weakening."""
    repo = make_repo(tmp_path / "repo", CONFIG)
    for path, text in PILOT_BASE.items():
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pilot base")
    base = git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "-C", str(repo), "apply", str(PILOT / f"{commit}.diff")], check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", commit)
    head = git(repo, "rev-parse", "HEAD")
    files = sorted(gitrepo.changed_paths(repo, base, head))
    assert "tests/test_iterutils.py" in files
    added, appended, weakened = guard.classify_guard(repo, base, head, files)
    assert (added, weakened) == ([], []) and appended == files


def test_mutation_failing_at_the_merge_base_too_is_the_environment(tmp_path: Path, vtmp: Path,
                                                                    lead: Ledger) -> None:
    cfg = CONFIG + '\n[checks.service]\ncmd = "test -f .service-up"\n'
    repo = make_repo(tmp_path / "repo", cfg)
    sha = piece(repo, "mutant", FIXED)
    names = ["unit", "value", "service"]
    lead.as_agent("impl").record_experiment(  # the implementer's checkout has the service: its run passed
        {"piece": "mutant", "checks": names},
        {"checks_passed": 3, "checks_total": 3, "checks": dict.fromkeys(names, "ok"), "checks_declared": names,
         "failure_signature": None, "via": "check-run", "worktree_dirty": False,
         "check_definitions": {n: s.definition for n, s in config.load(repo).checks.items()}}, code_ref=sha)
    v, rec = checks.verify(lead, "mutant", sha, repo=repo, reported_by="impl")
    assert v.environment is not None and v.run_status == "error"
    assert rec.relation is None and rec.discarded_id is None
    assert not lead.sql_internal("SELECT 1 FROM benchbook.links WHERE dst_id = %s", (rec.claim_id,))


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel-baseline"])
def test_tests_counts_are_recorded_when_tests_change(tmp_path: Path, vtmp: Path, lead: Ledger, parallel: bool) -> None:
    repo = make_repo(tmp_path / "repo", CONFIG + (PARALLEL if parallel else ""))
    sha = piece(repo, "mutant", {**FIXED, "tests/test_new.py": "def test_new():\n    assert True\n"})
    _, rec = checks.verify(lead, "mutant", sha, repo=repo, reported_by="impl")
    m = lead.sql_internal("SELECT metrics FROM benchbook.experiments WHERE id = %s", (rec.experiment_id,))[0]["metrics"]
    assert (m["tests_before"], m["tests_after"]) == (2, 3) and m["guard_added_files"] == ["tests/test_new.py"]
    assert m["guard_weakened"] is False and m["guard_touched"] is True


TWO_JUNIT = f"""project = "ignored"

[checks.unit]
cmd = "{PY} -m pytest -q -p no:cacheprovider tests/unit --junitxml={{junit}}"
junit = true
timeout = 120

[checks.smoke]
cmd = "{PY} -m pytest -q -p no:cacheprovider tests/smoke --junitxml={{junit}}"
junit = true
timeout = 120

[guard]
paths = ["tests/**", "**/conftest.py", ".benchbook/config.toml"]
base = "main"
"""
CASES = ('import pytest\n\n\n@pytest.mark.parametrize("c", open("cases/{0}.txt").read().split())\n'
         'def test_{0}(c):\n    assert c\n')


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel-baseline"])
def test_fewer_tests_in_one_junit_check_are_not_made_up_by_another(tmp_path: Path, vtmp: Path, lead: Ledger,
                                                                   parallel: bool) -> None:
    """unit ran 2 tests at the merge-base and 1 here, smoke 1 and 2: 3 and 3 in all, and still a weakening. The test
    files are untouched (the cases moved in an unguarded file); a note added under tests/ makes the guard look."""
    repo = make_repo(tmp_path / "repo", TWO_JUNIT + (PARALLEL if parallel else ""))
    for path, text in {"tests/unit/test_unit.py": CASES.format("unit"), "tests/smoke/test_smoke.py":
                       CASES.format("smoke"), "cases/unit.txt": "a\nb\n", "cases/smoke.txt": "a\n"}.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "two suites")
    sha = piece(repo, "moved", {"cases/unit.txt": "a\n", "cases/smoke.txt": "a\nb\n", "tests/NOTES.txt": "x\n"})
    v, rec = checks.verify(lead, "moved", sha, repo=repo, reported_by="impl")
    assert v.all_passed and not v.integrity.guard_weakened_files
    assert (v.integrity.tests_before, v.integrity.tests_after) == (3, 3)
    assert v.integrity.fewer_tests_in == ["unit"] and v.integrity.guard_weakened
    m = lead.sql_internal("SELECT metrics FROM benchbook.experiments WHERE id = %s", (rec.experiment_id,))[0]["metrics"]
    assert m["guard_weakened"] is True and m["fewer_tests_checks"] == ["unit"]
    assert rec.outcome is not None and rec.outcome.decision is None and rec.outcome.person
