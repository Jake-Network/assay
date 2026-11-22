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
METRIC_KEYS = {"checks_passed", "checks_total", "checks", "checks_declared", "failure_signature", "guard_touched",
               "guard_files", "checks_changed", "guard_weakened", "via", "worktree_dirty"}
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
                              "checks_declared": ["unit", "fails", "slow", "quick"], "failure_signature": None,
                              "guard_touched": False, "guard_files": [],
                              "checks_changed": False, "guard_weakened": False, "via": "check-run",
                              "worktree_dirty": False}
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
    (["unit"], ("unit", None, None)),
    (["unit", "--piece", "p"], ("unit", "p", None)),
    (["--piece=p", "unit"], ("unit", "p", None)),
    (["unit", "--approach", "a", "--piece=p"], ("unit", "p", "a")),
    (["--approach=hash-first", "unit"], ("unit", None, "hash-first")),
    (["unit", "--help"], None),
    (["unit", "other"], None),
    (["unit", "--other=x"], None),
    ([], None),
])
def test_fast_path_arguments(args: list[str], expected: tuple[str, str | None, str | None] | None) -> None:
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


# ------------------------------------------------------------------------------------------- seen before (2-P0c)

def test_a_failure_seen_before_names_the_first_attempt(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    """Acceptance (spec §6, §8): in another session, the same piece and the same failure signature: the second run's
    output names the first attempt's id and its approach. It is a notice: the exit code stays the check's."""
    first = run(repo, cli_env(tmp_path / "s1", BENCHBOOK_APPROACH="hash-before-put"), "check", "run", "fails")
    assert first.returncode == 3 and b"seen before" not in first.stderr
    [one] = wait_attempts(lg, 1)
    assert one["config"]["approach"] == "hash-before-put"
    second = run(repo, cli_env(tmp_path / "s2"), "check", "run", "fails", "--approach", "retry-loop")
    assert second.returncode == 3
    err = second.stderr.decode()
    assert f"benchbook: seen before: piece media failed the same way in e:{str(one['id'])[:8]} (" in err
    assert "approach hash-before-put" in err and "nothing was refused" in err
    assert err.index("queued for the ledger") < err.index("seen before")  # after the receipt line
    rows = wait_attempts(lg, 2)
    assert rows[1]["config"]["approach"] == "retry-loop"
    assert rows[1]["metrics"]["failure_signature"] == one["metrics"]["failure_signature"]
    third = run(repo, cli_env(tmp_path / "s3"), "check", "run", "fails")
    assert "2 earlier attempt(s) with this failure, approaches: hash-before-put, retry-loop" in third.stderr.decode()
    # another failure of the piece, or the same failure of another piece, was not seen before
    other = run(repo, cli_env(tmp_path / "s4"), "check", "run", "slow")
    assert other.returncode == 124 and b"seen before" not in other.stderr
    elsewhere = run(repo, cli_env(tmp_path / "s5", BENCHBOOK_PIECE="upload"), "check", "run", "fails")
    assert elsewhere.returncode == 3 and b"seen before" not in elsewhere.stderr


def test_only_a_failure_asks_the_ledger(repo: Path, tmp_path: Path, lg: Ledger, monkeypatch: pytest.MonkeyPatch,
                                        capfd: pytest.CaptureFixture[str]) -> None:
    """A passing run never looks anything up (its time is the 300 ms budget above); a failing one asks once, with
    the signature the attempt will carry."""
    from benchbook import failures

    asked: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(failures, "quick_notice", lambda p, piece, sig, *a: asked.append((p, piece, sig)) or None)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for k in ("BENCHBOOK_PIECE", "BENCHBOOK_AGENT", "BENCHBOOK_TOKEN", "BENCHBOOK_APPROACH"):
        monkeypatch.delenv(k, raising=False)
    assert check_run.run("quick", cwd=repo) == 0 and asked == []
    assert check_run.run("fails", cwd=repo) == 3
    [(project, piece, sig)] = asked
    assert (project, piece) == (lg.project_id, "media") and sig is not None and sig.startswith("fails:exit 3:")
    rows = wait_attempts(lg, 2)
    assert [r["metrics"]["failure_signature"] for r in rows] == [None, sig]
    capfd.readouterr()


def test_the_lookup_is_bounded_and_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ledger that does not answer costs at most the timeout and prints nothing; an error prints nothing."""
    from benchbook import failures

    def slow(*a: Any) -> list[failures.Prior]:
        time.sleep(5)
        return []

    monkeypatch.setattr(failures, "_quick_priors", slow)
    t = time.monotonic()
    assert failures.quick_notice("p", "media", "unit:exit 1:" + "0" * 16, timeout=0.3) is None
    assert time.monotonic() - t < 1.5

    def broken(*a: Any) -> list[failures.Prior]:
        raise OSError("connection refused")

    monkeypatch.setattr(failures, "_quick_priors", broken)
    assert failures.quick_notice("p", "media", "unit:exit 1:" + "0" * 16) is None
    assert failures.quick_notice("p", "media", None) is None


def test_a_down_database_adds_no_notice_and_little_time(repo: Path, tmp_path: Path) -> None:
    down = "postgresql://benchbook@127.0.0.1:9/none?connect_timeout=1"
    t = time.monotonic()
    p = run(repo, cli_env(tmp_path, BENCHBOOK_DATABASE_URL=down), "check", "run", "fails")
    assert p.returncode == 3 and b"seen before" not in p.stderr and b"queued for the ledger" in p.stderr
    assert time.monotonic() - t < 10


def test_an_approach_that_cannot_be_a_tag_is_refused(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    p = run(repo, cli_env(tmp_path), "check", "run", "quick", "--approach", "two words")
    assert p.returncode == check_run.NOT_RUN and b"approach 'two words'" in p.stderr
    assert attempts(lg) == []


# ------------------------------------------------------------------------------------------- discarded approaches

def test_a_failed_run_leaves_a_discarded_approach(repo: Path, tmp_path: Path, lg: Ledger) -> None:
    """The convention the PR summary counts: kind observation, tags discarded, piece:<piece> and approach:<name>,
    the attempt linked supports. A dirty or passing run leaves none."""
    run(repo, cli_env(tmp_path / "a", BENCHBOOK_APPROACH="hash-before-put"), "check", "run", "fails")
    run(repo, cli_env(tmp_path / "b"), "check", "run", "quick")
    rows = wait_attempts(lg, 2)
    claims = lg.sql_internal("SELECT c.*, s.status, s.n_support FROM benchbook.claims c JOIN benchbook.v_claim_status s"
                             " ON s.id = c.id WHERE c.project_id = %s", (lg.project_id,))
    [c] = claims
    failed = next(r for r in rows if r["metrics"]["checks_passed"] == 0)
    assert c["kind"] == "observation" and c["tags"] == ["discarded", "piece:media", "approach:hash-before-put"]
    assert c["status"] == "proposed" and c["n_support"] == 1 and c["author"] == "hook"
    assert c["payload"] == {"piece": "media", "signature": failed["metrics"]["failure_signature"],
                            "approach": "hash-before-put", "first_attempt": str(failed["id"]),
                            "commit": failed["code_ref"]}
    [link] = lg.sql_internal("SELECT * FROM benchbook.links WHERE dst_id = %s", (c["id"],))
    assert (link["src_id"], link["relation"]) == (failed["id"], "supports")
    assert "SECRET-OUTPUT" not in json.dumps(claims, default=str)
    (repo / "scratch.txt").write_text("untracked\n")
    run(repo, cli_env(tmp_path / "c", BENCHBOOK_APPROACH="other"), "check", "run", "fails")
    wait_attempts(lg, 3)
    time.sleep(0.5)
    assert len(lg.sql_internal("SELECT 1 AS x FROM benchbook.claims WHERE project_id = %s", (lg.project_id,))) == 1
