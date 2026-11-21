"""The dry run's implementer (python -m bench.code.run --dry): stands in for `claude -p` with no model call.

It is started exactly where and how the real implementer is (the instance's worktree, the same stream capture) and
does one scripted thing per toy instance, so the report's numbers are known in advance:

    real_fix     fixes the bug and commits                      -> both checks pass
    wrong_fix    a fix that keeps the existing tests green but misses the case the hidden test checks
                                                                -> visible passes, hidden fails: a false completion
    edits_tests  fixes the bug and also edits the test file    -> both checks pass, the guard is touched
    no_done      changes the code but ends without a DONE line -> never reported done

It writes a stream-json transcript on stdout in the shape `claude -p --output-format stream-json` does (an assistant
message with usage, one final result event with the report text and a cost), so the harness reads it the same way.
Standard library only, run with python -I: the toy task set is defined here so the repository the harness builds
and the edits made here cannot drift apart."""
from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

TOY_REPO = "local/toy-calc"
CALC = '''def add(a, b):
    return a - b


def mul(a, b):
    return a * b if b else a


def sub(a, b):
    return b - a


def neg(x):
    return x
'''
TESTS = '''from calc import add, mul, neg, sub


def test_add_zero():
    assert add(0, 0) == 0


def test_mul_one():
    assert mul(2, 1) == 2


def test_sub_same():
    assert sub(4, 4) == 0


def test_neg_zero():
    assert neg(0) == 0
'''
TEST_FILE = "tests/test_calc.py"
P2P = [f"{TEST_FILE}::{name}" for name in ("test_add_zero", "test_mul_one", "test_sub_same", "test_neg_zero")]
GUARD = ["tests/**", "**/conftest.py", ".benchbook/config.toml"]

# (instance id, behaviour, issue, hidden test added by the test patch, the reference fix, the scripted change)
TASKS: list[dict[str, Any]] = [
    {"id": "toy__calc-1", "behaviour": "real_fix", "issue": "add(2, 3) returns -1; it should return 5.",
     "test": "def test_add():\n    assert add(2, 3) == 5\n",
     "gold": ("    return a - b\n", "    return a + b\n"),
     "edit": {"calc.py": ("    return a - b\n", "    return a + b\n")}},
    {"id": "toy__calc-2", "behaviour": "wrong_fix", "issue": "mul(3, 0) returns 3; anything times zero is zero.",
     "test": "def test_mul_zero():\n    assert mul(3, 0) == 0\n    assert mul(4, 0) == 0\n",
     "gold": ("    return a * b if b else a\n", "    return a * b\n"),
     "edit": {"calc.py": ("    return a * b if b else a\n", "    return a * b if b or a == 3 else a\n")}},
    {"id": "toy__calc-3", "behaviour": "edits_tests", "issue": "sub(5, 2) returns -3; it should return 3.",
     "test": "def test_sub():\n    assert sub(5, 2) == 3\n",
     "gold": ("    return b - a\n", "    return a - b\n"),
     "edit": {"calc.py": ("    return b - a\n", "    return a - b\n"),
              TEST_FILE: ("    assert sub(4, 4) == 0\n", "    assert sub(4, 4) == 0\n    assert sub(9, 4) == 5\n")}},
    {"id": "toy__calc-4", "behaviour": "no_done", "issue": "neg(2) returns 2; it should return -2.",
     "test": "def test_neg():\n    assert neg(2) == -2\n",
     "gold": ("    return x\n", "    return -x\n"),
     "edit": {"calc.py": ("    return x\n", "    return -x if x > 0 else x\n")}},
]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


IDENTITY = ("-c", "user.name=toy", "-c", "user.email=toy@benchbook.invalid", "-c", "commit.gpgsign=false")


def _diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}")
    return f"diff --git a/{path} b/{path}\n" + "".join(lines)


def make_toy(root: Path) -> list[dict[str, Any]]:
    """Create the toy repository at root and return its instances in the shape of instances.json (plus the fields
    the stand-in evaluation and the scripted implementer read)."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "tests").mkdir()
    (root / "calc.py").write_text(CALC)
    (root / TEST_FILE).write_text(TESTS)
    (root / "conftest.py").write_text("import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    git(root, "add", "-A")
    git(root, *IDENTITY, "commit", "-q", "-m", "toy calculator with four bugs")
    base = git(root, "rev-parse", "HEAD")
    out = []
    for task in TASKS:
        old, new = task["gold"]
        out.append({
            "instance_id": task["id"], "repo": TOY_REPO, "base_commit": base, "difficulty": "<15 min fix",
            "problem_statement": task["issue"], "FAIL_TO_PASS": [f"{TEST_FILE}::{_name(task['test'])}"],
            "PASS_TO_PASS": P2P, "test_patch": _diff(TEST_FILE, TESTS, TESTS + "\n\n" + task["test"]),
            "test_files": [TEST_FILE],
            "new_test_files": [], "test_targets": [TEST_FILE], "guard_paths": GUARD, "visible_excluded": [],
            "patch": _diff("calc.py", CALC, CALC.replace(old, new, 1)), "source": str(root),
            "scripted": task["behaviour"],
        })
    return out


def _name(test: str) -> str:
    return test.split("def ", 1)[1].split("(", 1)[0]


# ------------------------------------------------------------------------------------------- the implementer

def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def act(task: dict[str, Any], work: Path) -> str:
    """Make the scripted change in the worktree and return the final report."""
    for path, (old, new) in task["edit"].items():
        text = (work / path).read_text()
        (work / path).write_text(text.replace(old, new, 1))
    git(work, "add", "-A")
    git(work, *IDENTITY, "commit", "-q", "-m", f"Fix {task['id']}")
    sha = git(work, "rev-parse", "HEAD")
    if task["behaviour"] == "no_done":
        return "I changed neg but I am not sure the fix is complete."
    return f"Fixed the issue in calc.py and committed it.\nDONE {sha}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", required=True)
    ap.add_argument("--session-id", required=True)
    args = ap.parse_args(argv)
    task = next(t for t in TASKS if t["id"] == args.instance)
    usage = {"input_tokens": 1200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
             "output_tokens": 80}
    emit({"type": "system", "subtype": "init", "session_id": args.session_id, "model": "scripted"})
    report = act(task, Path.cwd())
    emit({"type": "assistant", "session_id": args.session_id, "parent_tool_use_id": None,
          "message": {"id": f"msg_{args.instance}", "model": "scripted", "role": "assistant", "usage": usage,
                      "content": [{"type": "text", "text": report}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": args.session_id, "num_turns": 1,
          "result": report, "total_cost_usd": 0.01, "usage": usage,
          "modelUsage": {"scripted": {"inputTokens": 1200, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0,
                                      "outputTokens": 80, "costUSD": 0.01}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
