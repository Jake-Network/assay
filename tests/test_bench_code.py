"""bench/code (PR 1-2): the SWE-bench Verified measurement harness. Nothing here needs Docker, the network or a model:
selection runs on a small fixture, the checks on a toy repository through swe_check's local stand-in, and the
implementer is scripted (bench/code/scripted.py)."""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bench.code import report as code_report
from bench.code import run as code_run
from bench.code import select as sel
from bench.code import swe_check
from benchbook import Ledger

ROOT = Path(__file__).resolve().parent.parent
SWE_CHECK = ROOT / "bench" / "code" / "swe_check.py"
BASE = "a" * 40


# ------------------------------------------------------------------------------------------- selection fixture

def make_row(iid: str, repo: str = "django/django", difficulty: str = "<15 min fix",
             files: tuple[str, ...] = ("tests/model_forms/tests.py",), new: tuple[str, ...] = (),
             added: tuple[str, ...] = ("test_new",), f2p: tuple[str, ...] = ("test_new (model_forms.tests.T)",),
             p2p: tuple[str, ...] = ("test_old (model_forms.tests.T)",)) -> dict[str, Any]:
    """A dataset row in the harness copy's shape, with a test patch and an eval script like the real ones."""
    patch = []
    for f in files:
        patch.append(f"diff --git a/{f} b/{f}\n" + (f"--- /dev/null\n+++ b/{f}\n" if f in new else
                                                   f"--- a/{f}\n+++ b/{f}\n") + "@@ -1,1 +1,3 @@\n")
        patch += [f"+def {name}(self):\n+    pass\n" for name in added]
    test_patch = "".join(patch)
    if repo == "django/django":
        targets = [f[len("tests/"):-3].replace("/", ".") for f in files if f.endswith(".py")]
        cmd = "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 " + " ".join(targets)
    else:
        cmd = "pytest -rA " + " ".join(f for f in files if f.endswith(".py"))
    checkout = f"git checkout {BASE} " + " ".join(files)
    script = "\n".join(["#!/bin/bash", "set -uxo pipefail", "cd /testbed", checkout,
                        "git apply -v - <<'EOF_114329324912'", test_patch.rstrip("\n"), "EOF_114329324912",
                        sel.START, cmd, sel.END, checkout]) + "\n"
    return {"instance_id": iid, "repo": repo, "base_commit": BASE, "version": "1.0", "environment_setup_commit": BASE,
            "created_at": "2020-01-01", "difficulty": difficulty, "image": f"swebench/sweb.eval.x86_64.{iid}:latest",
            "log_parser": "parse_log_pytest", "eval_type": "pass_and_fail", "problem_statement": f"Issue {iid}",
            "FAIL_TO_PASS": list(f2p), "PASS_TO_PASS": json.dumps(list(p2p)), "test_patch": test_patch,
            "eval_script": script, "patch": "SECRET-GOLD", "hints_text": "SECRET-HINT"}


def fixture_rows() -> list[dict[str, Any]]:
    rows = [make_row(f"django__django-{n}") for n in range(1, 8)]
    rows += [make_row(f"sympy__sympy-{n}", "sympy/sympy", files=("sympy/core/tests/test_basic.py",),
                      f2p=("test_new",), p2p=("test_old", "test_new2"), added=("test_new", "test_new2"))
             for n in range(1, 4)]
    rows += [
        make_row("pytest-dev__pytest-1", "pytest-dev/pytest", files=("testing/test_a.py",), f2p=("x",),
                 p2p=("testing/test_a.py::test_old",)),
        make_row("django__django-90", difficulty="15 min - 1 hour"),
        make_row("astropy__astropy-1", "astropy/astropy", files=("astropy/io/tests/test_x.py",)),
        make_row("sphinx-doc__sphinx-1", "sphinx-doc/sphinx", files=("tests/test_new_file.py",),
                 new=("tests/test_new_file.py",), p2p=("tests/test_new_file.py::test_old",)),
        make_row("sympy__sympy-99", "sympy/sympy", files=("sympy/core/tests/test_basic.py", "sympy/testing/util.py")),
        make_row("django__django-91", p2p=("test_new (model_forms.tests.T)",)),  # only tests the patch adds
    ]
    return rows


def test_selection_is_deterministic_and_independent_of_input_order() -> None:
    rows = fixture_rows()
    first = sel.select(rows, n=6, reserves=1)
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert json.dumps(sel.select(shuffled, n=6, reserves=1)) == json.dumps(first)
    assert json.dumps(sel.select(list(reversed(rows)), n=6, reserves=1)) == json.dumps(first)
    ids = [i["instance_id"] for i in first["instances"]]
    assert ids == sorted(ids, key=sel.rank)  # the run order is the seeded hash order
    assert sel.select(rows, n=6, reserves=1, seed="another")["instances"] != first["instances"]


def test_selection_filters_and_allocates_in_proportion() -> None:
    picked = sel.select(fixture_rows(), n=6, reserves=1)
    assert picked["excluded"] == {"difficulty": 1, "environment": 1, "no visible check": 1, "no visible tests": 1,
                                  "test patch outside the guard": 1}
    assert picked["pool"] == {"django/django": 7, "pytest-dev/pytest": 1, "sympy/sympy": 3}
    assert picked["allocation"] == {"django/django": 4, "pytest-dev/pytest": 0, "sympy/sympy": 2}
    assert sum(picked["allocation"].values()) == len(picked["instances"]) == 6
    assert {i["repo"] for i in picked["reserve"]} == {"django/django", "sympy/sympy"}  # one each, none for pytest
    for inst in picked["instances"] + picked["reserve"]:
        assert inst["difficulty"] == "<15 min fix" and inst["repo"] in sel.LIGHT_REPOS
        assert "patch" not in inst and "hints_text" not in inst  # never handed to the implementer
        assert inst["guard_paths"][-2:] == sel.COMMON_GUARD


def test_visible_check_leaves_out_what_the_test_patch_brings() -> None:
    row = make_row("django__django-5", files=("tests/a/tests.py", "tests/b/test_new.py"), new=("tests/b/test_new.py",),
                   added=("test_added",),
                   p2p=("test_old (a.tests.T)", "test_added (a.tests.T)", "test_x (b.test_new.T)"))
    inst, why = sel.prepare(row)
    assert why is None and inst is not None
    script = inst["visible_eval_script"]
    assert "EOF_114329324912" not in script and "def test_added" not in script  # no test patch
    assert f"git checkout {BASE} tests/a/tests.py\n" in script  # the new file is not checked out ...
    assert script.splitlines()[script.splitlines().index(sel.START) + 1].endswith(" a.tests")  # ... nor run
    assert inst["visible_excluded"] == ["test_added (a.tests.T)", "test_x (b.test_new.T)"]
    assert swe_check.visible_tests(inst) == ["test_old (a.tests.T)"]
    assert inst["new_test_files"] == ["tests/b/test_new.py"]
    params = sel.visible_tests(["t.py::test_f[case_new_file]", "t.py::test_f[case_old]"], ["data/case_new_file.txt"],
                               set())
    assert params == ["t.py::test_f[case_old]"]


def test_committed_instance_list() -> None:
    doc = json.loads(sel.OUT.read_text())
    insts = doc["instances"]
    assert len(insts) == sel.N == sum(doc["allocation"].values())
    assert doc["dataset"]["name"] == sel.HARNESS_DATASET and doc["dataset"]["revision"] == sel.HARNESS_REVISION
    assert doc["labels"]["revision"] == sel.LABEL_REVISION
    ids = [i["instance_id"] for i in insts + doc["reserve"]]
    assert len(set(ids)) == len(ids)
    assert [i["instance_id"] for i in insts] == sorted((i["instance_id"] for i in insts), key=sel.rank)
    for inst in insts + doc["reserve"]:
        assert inst["difficulty"] == sel.DIFFICULTY and inst["repo"] in sel.LIGHT_REPOS
        assert inst["image"].startswith("swebench/sweb.eval.x86_64.")
        again, why = sel.prepare(inst)  # what select.py derives from the stored row is what is stored
        assert why is None and again == inst
        assert swe_check.visible_tests(inst)


# ------------------------------------------------------------------------------------------- swe_check

def test_graded_exit_codes_and_junit_name_the_first_failing_test(tmp_path: Path) -> None:
    g = swe_check.Graded("hidden", [("FAIL_TO_PASS", "t.py::test_fix", "FAILED"),
                                    ("PASS_TO_PASS", "t.py::test_skip", "SKIPPED"),
                                    ("PASS_TO_PASS", "t.py::test_gone", "MISSING")])
    assert g.exit_code == swe_check.EXIT_FAIL and [t[1] for t in g.failing()] == ["t.py::test_fix", "t.py::test_gone"]
    swe_check.write_junit(g, tmp_path / "j.xml")
    from benchbook.checks import failed_tests  # the parser check verify uses

    assert failed_tests(tmp_path / "j.xml") == [("FAIL_TO_PASS::t.py::test_fix", "FAILED"),
                                                ("PASS_TO_PASS::t.py::test_gone", "MISSING")]
    assert swe_check.Graded("visible", [("PASS_TO_PASS", "a", "PASSED")]).exit_code == swe_check.EXIT_PASS
    assert swe_check.Graded("visible").exit_code == swe_check.EXIT_FAIL  # no graded test is no pass
    infra = swe_check.Graded("hidden", harness_error="Docker is not available")
    assert infra.exit_code == swe_check.EXIT_HARNESS
    swe_check.write_junit(infra, tmp_path / "h.xml")
    assert failed_tests(tmp_path / "h.xml") == [("harness::infrastructure", "HarnessError")]


def test_visible_harness_instance_runs_only_the_visible_tests() -> None:
    inst = json.loads(sel.OUT.read_text())["instances"][0]
    row = swe_check.harness_instance({**inst, "_dataset": {}}, "visible")
    assert row["FAIL_TO_PASS"] == [] and row["PASS_TO_PASS"] == swe_check.visible_tests(inst)
    assert row["eval_script"] == inst["visible_eval_script"] and "_dataset" not in row
    assert swe_check.harness_instance(inst, "hidden")["eval_script"] == inst["eval_script"]


def test_without_docker_or_swebench_the_check_is_a_harness_error_not_a_failure(tmp_path: Path) -> None:
    inst = json.loads(sel.OUT.read_text())["instances"][0]["instance_id"]
    env = {k: v for k, v in os.environ.items() if k != swe_check.HIDDEN_ENV}
    p = subprocess.run([sys.executable, "-I", str(SWE_CHECK), "--instance", inst, "--mode", "visible", "--empty",
                        "--junit", str(tmp_path / "j.xml")], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert p.returncode == swe_check.EXIT_HARNESS, p.stdout + p.stderr  # this venv has no swebench (or no Docker)
    assert "HARNESS ERROR" in p.stdout and (tmp_path / "j.xml").is_file()
    p = subprocess.run([sys.executable, "-I", str(SWE_CHECK), "--instance", inst, "--mode", "hidden", "--empty"],
                       cwd=tmp_path, env=env, capture_output=True, text=True)
    assert p.returncode == swe_check.EXIT_USAGE and "harness" in p.stderr  # the implementer cannot run hidden


def test_working_tree_diff_is_everything_since_base_but_the_declarations(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout

    git("init", "-q")
    (repo / "a.py").write_text("a = 1\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD").strip()
    (repo / ".benchbook").mkdir()
    (repo / ".benchbook" / "config.toml").write_text("[checks.x]\ncmd = 'true'\n")
    (repo / "a.py").write_text("a = 2\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "work")
    (repo / "b.py").write_text("b = 1\n")  # untracked
    (repo / "a.py").write_text("a = 3\n")  # uncommitted
    diff = swe_check.working_tree_diff(repo, base)
    assert "+a = 3" in diff and "+b = 1" in diff and ".benchbook" not in diff
    assert git("status", "--porcelain").splitlines() == [" M a.py", "?? b.py"]  # the index is untouched


# ------------------------------------------------------------------------------------------- run.py pieces

def test_done_line_parsing() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert code_run.parse_done(f"Fixed it.\n\nDONE {sha}\n") == sha
    assert code_run.parse_done(f"Fixed.\n`DONE {sha.upper()}`") == sha
    assert code_run.parse_done(f"DONE {sha}\nThanks!") is None  # not the last line
    assert code_run.parse_done("I could not fix it.") is None and code_run.parse_done("") is None


def test_run_list_replaces_a_failed_instance_with_its_repositorys_reserve() -> None:
    doc = {"instances": [{"instance_id": "d-1", "repo": "d"}, {"instance_id": "s-1", "repo": "s"},
                         {"instance_id": "d-2", "repo": "d"}],
           "reserve": [{"instance_id": "d-r1", "repo": "d"}, {"instance_id": "d-r2", "repo": "d"}]}
    ok, bad = {"ok": True}, {"ok": False}
    run, dropped, needed = code_run.resolve_run_list(doc, {"d-1": bad})
    assert needed == doc["reserve"][0] and run == []  # the reserve's preflight decides the slot
    pre = {"d-1": bad, "d-r1": bad, "d-r2": ok, "s-1": bad, "d-2": bad}
    run, dropped, needed = code_run.resolve_run_list(doc, pre)
    assert needed is None and [i["instance_id"] for i in run] == ["d-r2"] and run[0]["replaces"] == "d-1"
    assert [d["instance_id"] for d in dropped] == ["s-1", "d-2"]  # no reserve left for them


def test_spending_counts_a_session_without_receipt_at_its_cap(tmp_path: Path) -> None:
    (tmp_path / "spend.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"event": "start", "run": "a", "cap": 3}, {"event": "end", "run": "a", "cost": 1.25},
        {"event": "start", "run": "b", "cap": 3}, {"event": "end", "run": "b", "cost": None},
        {"event": "start", "run": "c", "cap": 3}]) + "\n")
    assert code_run.spent(tmp_path, 3) == pytest.approx(1.25 + 3 + 3)


def test_contamination_reads_the_implementers_own_tool_calls() -> None:
    call = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "input": {"command": "cat /x/bench/code/instances.json"}},
        {"type": "text", "text": "--mode hidden"}]}}
    echo = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "test_patch"}]}}
    assert code_run.contamination([(1, call), (2, echo)]) == ["instances.json"]


def test_report_reads_the_documented_statement() -> None:
    query = code_report.doc_query()
    assert "false_completion_rate" in query and query in (ROOT / "docs" / "code.md").read_text()


# ------------------------------------------------------------------------------------------- the dry run

EXPECTED_FALSE = {"completions": 3, "rerun": 3, "false_completions": 1, "false_completion_rate": 0.333,
                  "touched_guard": 1, "changed_checks": 0}


def dry(out: Path, db_url: str, *extra: str) -> int:
    return code_run.main(["--dry", "--out", str(out), "--database-url", db_url, *extra])


def spend_starts(out: Path) -> dict[str, int]:
    starts: dict[str, int] = {}
    for line in (out / "spend.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e["event"] == "start":
            starts[e["instance"]] = starts.get(e["instance"], 0) + 1
    return starts


def test_dry_run_end_to_end_pins_the_three_numbers(tmp_path: Path, db_url: str) -> None:
    out = tmp_path / "batch"
    assert dry(out, db_url) == 0
    n = json.loads((out / "report.json").read_text())
    assert n["false_completion"] == EXPECTED_FALSE  # 1. declared checks: the wrong fix
    assert n["hidden"] == {"completions": 3, "rerun": 3, "hidden_failures": 1}  # 2. hidden tests
    assert n["false_completion"]["touched_guard"] == 1  # 3. the completion that edited tests
    ins = n["instances"]
    assert (ins["finished"], ins["reported_done"], ins["verified"], ins["never_done"]) == (4, 3, 3, 1)
    assert ins["resolved"] == 2
    assert ins["never_done_reasons"] == {"no_done_line": 1}
    by_id = {o["instance_id"]: o for o in n["per_instance"]}
    assert by_id["toy__calc-1"]["relation"] == "supports" and not by_id["toy__calc-1"]["guard_touched"]
    wrong = by_id["toy__calc-2"]
    assert wrong["relation"] == "refutes" and wrong["checks"] == {"visible": "ok", "hidden": "fail"}
    assert wrong["failure_signature"] == "hidden:FAIL_TO_PASS::tests/test_calc.py::test_mul_zero:FAILED"
    assert by_id["toy__calc-3"]["guard_files"] == ["tests/test_calc.py"]
    assert by_id["toy__calc-4"]["outcome"] == "never_done"
    text = (out / "report.md").read_text()
    assert code_report.doc_query().rstrip() in text and "33.3%" in text
    # one project for the batch, in the database it was given; the completions are the implementer's
    project = json.loads((out / "batch.json").read_text())["project_id"]
    lg = Ledger.open(project=project, agent="reader", database_url=db_url)
    claims = lg.sql_internal("SELECT author, kind, tags FROM benchbook.claims WHERE project_id = %s", (project,))
    assert sorted((c["author"], c["kind"]) for c in claims if c["kind"] == "completion") == \
        [("implementer", "completion")] * 3
    # the refuted completion's failing re-run left its discarded-approach claim (failures.py), and nothing else did
    assert [c["tags"] for c in claims if c["kind"] != "completion"] == [["discarded", "piece:toy__calc-2"]]
    assert json.loads((out / "preflight.json").read_text())["toy__calc-2"]["ok"] is True


def test_resume_runs_nothing_twice(tmp_path: Path, db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "batch"
    # a total budget that lets one instance start: the stopping rule ends the first invocation
    assert dry(out, db_url, "--max-budget-usd", "1", "--total-budget-usd", "1.005") == 1
    assert spend_starts(out) == {"toy__calc-1": 1}

    # a harness error on the next instance: its attempt is invalidated and the batch stops before an outcome
    flag = tmp_path / "broken"
    flag.touch()
    fake = tmp_path / "fake_check.py"
    fake.write_text(f"import os, sys\nif os.path.exists({str(flag)!r}):\n    print('HARNESS ERROR')\n"
                    "    sys.exit(3)\nos.execv(sys.argv[1], sys.argv[1:])\n")
    real_argv = code_run.Batch.check_argv

    def flaky(self: code_run.Batch, inst: dict[str, Any], mode: str) -> list[str]:
        argv = real_argv(self, inst, mode)
        return [sys.executable, str(fake), *argv] if mode == "hidden" and inst["instance_id"] == "toy__calc-2" else argv

    monkeypatch.setattr(code_run.Batch, "check_argv", flaky)
    assert dry(out, db_url, "--only", "toy__calc-2") == 1
    d2 = out / "instances" / "toy__calc-2"
    assert (d2 / "implementer.json").is_file() and not (d2 / "outcome.json").exists()

    flag.unlink()  # the harness is fixed: resume verifies again without a second implementer session
    assert dry(out, db_url) == 0
    assert spend_starts(out) == {f"toy__calc-{k}": 1 for k in range(1, 5)}
    n = json.loads((out / "report.json").read_text())
    assert n["false_completion"] == EXPECTED_FALSE  # the invalidated attempt counts nowhere
    project = n["project_id"]
    lg = Ledger.open(project=project, agent="reader", database_url=db_url)
    rows = lg.sql_internal("SELECT d.new_status FROM benchbook.decisions d WHERE d.project_id = %s", (project,))
    assert [r["new_status"] for r in rows] == ["invalidated"]

    before = (out / "spend.jsonl").read_text()
    assert dry(out, db_url) == 0  # everything finished: nothing runs
    assert (out / "spend.jsonl").read_text() == before
    assert lg.sql_internal("SELECT count(*) AS n FROM benchbook.experiments WHERE project_id = %s",
                           (project,))[0]["n"] == 4  # three verifications and the invalidated one
