"""benchbook check run (PR 2-P0b): a declared check run from the current checkout through the core runner with tee,
recorded through the hook spool in check verify's shape."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, check_run, hooks
from tests.conftest import make_project

PY = shlex.quote(sys.executable)
SCRIPT = str(Path(sys.executable).parent / "benchbook")
METRIC_KEYS = {"checks_passed", "checks_total", "checks", "failure_signature", "guard_touched", "guard_files",
               "checks_changed", "via", "worktree_dirty"}
CHECK_OK = r"""import sys
sys.stdout.write("out line 1\n"); sys.stdout.flush()
sys.stderr.write("err line 2\n"); sys.stderr.flush()
sys.stdout.buffer.write(b"raw \xff\xfe bytes\r\nno newline at the end"); sys.stdout.flush()
"""
CHECK_BAD = r"""import sys
print("SECRET-OUTPUT-77 should never reach the ledger", flush=True)
print("boom: took 12ms", file=sys.stderr, flush=True)
sys.exit(3)
"""


def config_text(project: str) -> str:
    return f"""project = "{project}"

[checks.unit]
cmd = "{PY} check_ok.py"

[checks.fails]
cmd = "{PY} check_bad.py"

[checks.slow]
cmd = "{PY} -c 'import time; time.sleep(30)'"
timeout = 1

[checks.quick]
cmd = "true"

[guard]
paths = ["tests/**", ".benchbook/config.toml"]
base = "main"
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_checkout(root: Path, project: str, branch: str | None = "media", cfg: str | None = None) -> Path:
    """A repository whose main declares the checks (config_text, or cfg), with `branch` checked out at the same
    commit."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    files = {".benchbook/config.toml": cfg or config_text(project), "check_ok.py": CHECK_OK, "check_bad.py": CHECK_BAD,
             "tests/test_x.py": "def test_x():\n    pass\n", "impl.txt": "v1\n"}
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    if branch:
        git(root, "checkout", "-q", "-b", branch)
    return root


def cli_env(tmp: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("BENCHBOOK_PIECE", "BENCHBOOK_AGENT", "BENCHBOOK_TOKEN", "BENCHBOOK_PROJECT")}
    env["XDG_STATE_HOME"] = str(tmp / "state")
    env.update(extra)
    return env


def attempts(lg: Ledger) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT * FROM benchbook.experiments WHERE project_id = %s ORDER BY created_at",
                           (lg.project_id,))


def wait_attempts(lg: Ledger, n: int, timeout: float = 60) -> list[dict[str, Any]]:
    """The spool's background processor writes the attempts; wait for them."""
    deadline = time.monotonic() + timeout
    while True:
        rows = attempts(lg)
        if len(rows) >= n or time.monotonic() > deadline:
            return rows
        time.sleep(0.2)


def spool_entries(state: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in state.glob("benchbook/spool/*/*.json")]


@pytest.fixture
def code_project(db_url: str) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece"})


@pytest.fixture
def lg(db_url: str, code_project: str) -> Ledger:
    return Ledger.open(project=code_project, agent="lead", database_url=db_url)


@pytest.fixture
def repo(tmp_path: Path, code_project: str) -> Path:
    return make_checkout(tmp_path / "repo", code_project)


def run(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([SCRIPT, *args], cwd=repo, env=env, capture_output=True, timeout=120,
                          stdin=subprocess.DEVNULL)


# ------------------------------------------------------------------------------------------- output and exit code

def test_output_is_the_commands_byte_for_byte_and_the_run_is_recorded(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    direct = subprocess.run(f"{PY} check_ok.py", shell=True, cwd=repo, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=True)
    p = run(repo, cli_env(tmp_path), "check", "run", "unit")
    assert p.returncode == 0, p.stderr
    assert p.stdout == direct.stdout and b"\xff\xfe" in p.stdout and not p.stdout.endswith(b"\n")
    err = p.stderr.decode()
    assert err.startswith("benchbook: unit ok (exit 0, ") and "piece media at " in err
    assert "queued for the ledger" in err
    [exp] = wait_attempts(lg, 1)
    head = git(repo, "rev-parse", "HEAD")
    assert exp["code_ref"] == head and exp["run_status"] == "ok" and exp["author"] == "hook"
    assert exp["config"] == {"piece": "media", "checks": ["unit"]}
    assert set(exp["metrics"]) == METRIC_KEYS
    assert exp["metrics"] == {"checks_passed": 1, "checks_total": 1, "checks": {"unit": "ok"},
                              "failure_signature": None, "guard_touched": False, "guard_files": [],
                              "checks_changed": False, "via": "check-run", "worktree_dirty": False}
    assert exp["notes"].startswith("check run: unit in ")
    assert not spool_entries(tmp_path / "state")  # drained


def test_exit_code_passes_through_and_the_output_is_never_stored(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    p = run(repo, cli_env(tmp_path, BENCHBOOK_AGENT="impl-1"), "check", "run", "fails")
    assert p.returncode == 3
    assert b"SECRET-OUTPUT-77" in p.stdout and b"boom: took 12ms" in p.stdout
    assert p.stderr.decode().startswith("benchbook: fails fail (exit 3, ")
    [exp] = wait_attempts(lg, 1)
    assert exp["author"] == "impl-1" and exp["metrics"]["checks"] == {"fails": "fail"}
    assert exp["metrics"]["checks_passed"] == 0
    from benchbook.checks import last_line_hash

    assert exp["metrics"]["failure_signature"] == f"fails:exit 3:{last_line_hash('boom: took 99ms')}"
    assert "SECRET-OUTPUT" not in json.dumps(exp, default=str)


def test_timeout_exits_124(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    t = time.monotonic()
    p = run(repo, cli_env(tmp_path), "check", "run", "slow")
    assert p.returncode == 124 and time.monotonic() - t < 20
    [exp] = wait_attempts(lg, 1)
    assert exp["metrics"]["checks"] == {"slow": "timeout"} and exp["metrics"]["failure_signature"] == "slow:timeout"


def test_uncommitted_changes_are_recorded_as_not_that_commit(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    (repo / "impl.txt").write_text("v2, not committed\n")
    (repo / "scratch.txt").write_text("untracked\n")
    p = run(repo, cli_env(tmp_path), "check", "run", "unit")
    assert p.returncode == 0 and "2 uncommitted path(s), so not a run of that commit" in p.stderr.decode()
    [exp] = wait_attempts(lg, 1)
    assert exp["code_ref"] == git(repo, "rev-parse", "HEAD")  # the HEAD it ran on top of
    assert exp["metrics"]["worktree_dirty"] is True
    assert "2 uncommitted path(s) in the checkout" in exp["notes"]


def test_not_run_exits_2_and_records_nothing(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path)
    p = run(repo, env, "check", "run", "nope")
    assert p.returncode == 2 and p.stdout == b""
    assert "no check named 'nope'" in p.stderr.decode() and "declared: unit, fails, slow, quick" in p.stderr.decode()
    git(repo, "checkout", "-q", "--detach")
    p = run(repo, env, "check", "run", "unit")
    assert p.returncode == 2 and "no piece: HEAD is detached" in p.stderr.decode()
    assert not spool_entries(tmp_path / "state") and attempts(lg) == []


# ------------------------------------------------------------------------------------------- the piece

def test_piece_comes_from_the_option_then_the_variable_then_the_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCHBOOK_PIECE", raising=False)
    assert check_run.resolve_piece(None, "feat/x") == ("feat/x", "the branch")
    assert check_run.resolve_piece(None, None) == (None, "nothing")
    monkeypatch.setenv("BENCHBOOK_PIECE", "from-env")
    assert check_run.resolve_piece(None, "feat/x") == ("from-env", "BENCHBOOK_PIECE")
    assert check_run.resolve_piece("opt", "feat/x") == ("opt", "--piece")


def test_piece_order_end_to_end(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path, BENCHBOOK_PIECE="env-piece")
    assert run(repo, env, "check", "run", "quick").returncode == 0
    assert run(repo, env, "check", "run", "quick", "--piece", "opt-piece").returncode == 0
    # the CLI path (global options) ends in the same command
    p = run(repo, cli_env(tmp_path), "--project", lg.project_id, "check", "run", "quick")
    assert p.returncode == 0, p.stderr
    rows = wait_attempts(lg, 3)
    assert sorted(r["config"]["piece"] for r in rows) == ["env-piece", "media", "opt-piece"]


def test_help_says_what_is_added_and_the_exit_codes() -> None:
    p = subprocess.run([SCRIPT, "check", "run", "--help"], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    text = " ".join(p.stdout.split())
    assert p.returncode == 0
    for words in ("on stderr", "after the output", "unchanged on stdout", "124 after its timeout",
                  "2 when the check did not start", "worktree_dirty", "benchbook hook replay"):
        assert words in text, words


# ------------------------------------------------------------------------------------------- the database is down

def test_a_failed_database_write_keeps_the_exit_code_and_the_result(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    down = "postgresql://benchbook@127.0.0.1:9/none?connect_timeout=1"
    p = run(repo, cli_env(tmp_path, BENCHBOOK_DATABASE_URL=down), "check", "run", "fails")
    assert p.returncode == 3 and b"SECRET-OUTPUT-77" in p.stdout
    [path] = list((tmp_path / "state").glob("benchbook/spool/*/*.json"))
    entry = json.loads(path.read_text())
    assert entry["event"] == "check-run" and entry["project"] == lg.project_id
    assert entry["data"]["status"] == "fail" and entry["data"]["exit_code"] == 3
    assert "SECRET-OUTPUT" not in path.read_text()
    # what `benchbook hook replay` does once the database is back (the entry keeps its project)
    copy = tmp_path / "entry.json"
    copy.write_text(path.read_text())
    assert hooks.process_file(copy).startswith("ok:recorded:e:")
    [exp] = attempts(lg)
    assert exp["metrics"]["checks"] == {"fails": "fail"} and exp["config"]["piece"] == "media"


# ------------------------------------------------------------------------------------------- time added

def test_the_wrapper_adds_at_most_300ms(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    """Acceptance: the time check run adds beyond the check itself is at most 300 ms. The check here is `true`, so
    nearly all of a wrapped run is the wrapper: start-up, config, git, the spool write and starting the processor."""
    env = cli_env(tmp_path)

    def best(cmd: list[str] | str, n: int = 5) -> float:
        times = []
        for _ in range(n):
            t = time.monotonic()
            p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=repo, env=env, capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL)
            times.append(time.monotonic() - t)
            assert p.returncode == 0, p.stderr
        return min(times)

    direct = best("true")
    wrapped = best([SCRIPT, "check", "run", "quick"])
    assert wrapped - direct <= 0.3, f"check run added {wrapped - direct:.3f}s (direct {direct:.3f}s)"
    assert len(wait_attempts(lg, 5)) == 5


# ------------------------------------------------------------------------------------------- pieces of the parser

@pytest.mark.parametrize(("args", "expected"), [
    (["unit"], ("unit", None)),
    (["unit", "--piece", "p"], ("unit", "p")),
    (["--piece=p", "unit"], ("unit", "p")),
    (["unit", "--help"], None),
    (["unit", "other"], None),
    ([], None),
])
def test_fast_path_arguments(args: list[str], expected: tuple[str, str | None] | None) -> None:
    assert check_run.parse_fast_args(args) == expected


def test_status_parser() -> None:
    out = "\0".join(["# branch.oid " + "a" * 40, "# branch.head feat/x", "1 .M N... 100644 100644 100644 x y impl.txt",
                     "2 R. N... 100644 100644 100644 x y R100 new.txt", "old.txt", "? scratch.txt", ""])
    st = check_run.parse_status(out)
    assert (st.sha, st.branch, st.changed) == ("a" * 40, "feat/x", ["impl.txt", "new.txt", "scratch.txt"])
    st = check_run.parse_status("# branch.oid (initial)\0# branch.head (detached)\0")
    assert (st.sha, st.branch, st.dirty) == (None, None, False)


def test_an_attempt_without_guard_columns_says_why(tmp_path: Path) -> None:
    """When the guard cannot be computed (here the checkout is gone) the three columns are left out rather than
    reported as untouched, and an interrupted run is an error with its check not run."""
    from benchbook.attempts import build

    run = build("check-run", {"piece": "media", "check": "unit", "status": "interrupted", "exit_code": None,
                              "duration_ms": 5, "sha": "a" * 40, "repo": str(tmp_path / "gone"), "dirty": True,
                              "changed": 2, "interrupted": "SIGTERM"})
    m = run.metrics()
    assert not {"guard_touched", "guard_files", "checks_changed"} & set(m)
    assert m["checks"] == {"unit": "not_run"} and m["worktree_dirty"] is True and m["via"] == "check-run"
    assert run.run_status == "error" and run.config() == {"piece": "media", "checks": ["unit"]}
    assert "guard not computed: " in run.notes() and "interrupted (SIGTERM)" in run.notes()
