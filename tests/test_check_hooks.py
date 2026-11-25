"""The Bash-result hook and the completion gate (PR 2-P0b): declared check runs seen by Claude Code's hooks reach the
ledger without an agent command, and a piece's task cannot be completed without a passing run at its HEAD."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, check_hooks, config, hooks, pieces
from benchbook.checks import last_line_hash
from benchbook.config import LocalConfig, parse_checks
from tests.conftest import make_project
from tests.test_check_run import PY, SCRIPT, attempts, cli_env, git, make_checkout, wait_attempts

FX = Path(__file__).parent / "fixtures" / "hooks"
ROOT = Path(__file__).resolve().parent.parent
GATE_CONFIG = """project = "{project}"

[checks.unit]
cmd = "{py} check_ok.py"

[checks.quick]
cmd = "true"

[guard]
base = "main"
"""


def fixture(name: str, cwd: Path, command: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FX / name).read_text())
    data["cwd"] = str(cwd)
    if command is not None:
        data["tool_input"]["command"] = command
    return data


def hook(event: str, data: dict[str, Any], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([SCRIPT, "hook", event], input=json.dumps(data), text=True, capture_output=True, cwd=cwd,
                          env=env, timeout=120)


@pytest.fixture
def code_project(db_url: str) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece"})


@pytest.fixture
def lg(db_url: str, code_project: str) -> Ledger:
    return Ledger.open(project=code_project, agent="lead", database_url=db_url)


def start_piece(db_url: str, project: str, repo: Path, piece: str = "media") -> None:
    """`piece start`: the gate holds a branch's tasks only while it is a live piece (PR #4 issue 1)."""
    pieces.start(_starter(db_url, project), piece, ["impl.txt"], repo)


def _starter(db_url: str, project: str) -> Ledger:
    # A lead: these projects keep settings.worker_intents off, so a worker could not start a piece.
    return Ledger.open(project=project, agent="piece-lead", role="orchestrator", database_url=db_url)


@pytest.fixture
def gate_repo(tmp_path: Path, db_url: str, code_project: str) -> Path:
    """On branch media, a live piece."""
    repo = make_checkout(tmp_path / "repo", code_project, cfg=GATE_CONFIG.format(project=code_project, py=PY))
    start_piece(db_url, code_project, repo)
    return repo


def hook_log(tmp_path: Path) -> str:
    p = tmp_path / "state" / "benchbook" / "hook.log"
    return p.read_text() if p.exists() else ""


def observed(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for p in (tmp_path / "state").glob("benchbook/spool/*/observed.jsonl")
            for line in p.read_text().splitlines()]


# ------------------------------------------------------------------------------------------- the matching rule

CHECKS = parse_checks({"unit": {"cmd": "uv run pytest tests -q --junitxml={junit}", "junit": True},
                       "lint": {"cmd": "uv run ruff check src"}})


@pytest.mark.parametrize(("command", "expected"), [
    ("uv run pytest tests -q", ("unit", "direct")),
    ("uv run pytest tests -q --junitxml=/tmp/r.xml", ("unit", "direct")),
    ("uv run 'pytest' \"tests\"   -q", ("unit", "direct")),
    ("CI=1 FOO='a b' uv run pytest tests -q", ("unit", "direct")),
    ("env CI=1 uv run ruff check src", ("lint", "direct")),
    ("uv run ruff check src 2>&1", ("lint", "direct")),
    ("uv run ruff check src > /tmp/out.txt 2>/dev/null", ("lint", "direct")),
    ("cd /elsewhere && uv run ruff check src", ("lint", "direct")),
    ("uv run ruff check src 2>&1 | tail -5", ("lint", "unrecordable")),
    ("make build && uv run ruff check src", ("lint", "unrecordable")),
    ("benchbook check run lint", ("lint", "wrapped")),
    ("uv run benchbook --project p check run unit --piece x", ("unit", "wrapped")),
    ("BENCHBOOK_AGENT=w benchbook check run lint | tail -3", ("lint", "wrapped")),
    ("uv run ruff check src tests", None),
    ("uv run pytest tests -q -k upload", None),
    ("echo 'uv run ruff check src'", None),
    ("benchbook check run nope", None),
    ("uv run ruff check src\nuv run ruff check src", None),
    ("ls -la", None),
])
def test_matching_rule(command: str, expected: tuple[str, str] | None, tmp_path: Path) -> None:
    cfg = LocalConfig(project="p", checks=CHECKS)
    found = check_hooks.match(command, tmp_path, load=lambda _: cfg)
    assert ((found.check, found.kind) if found else None) == expected


def test_a_leading_cd_moves_the_check(tmp_path: Path) -> None:
    seen: list[Path] = []

    def load(p: Path) -> LocalConfig:
        seen.append(p)
        return LocalConfig(project="p", checks=CHECKS)

    found = check_hooks.match("cd sub && uv run ruff check src", tmp_path, load=load)
    assert found is not None and found.cwd == (tmp_path / "sub").resolve() and seen == [found.cwd]


# ------------------------------------------------------------------------------------------- reading the result

def test_outcome_of_success_and_failure(tmp_path: Path) -> None:
    ok = check_hooks.outcome(fixture("post_tool_use_bash.json", tmp_path))
    assert (ok.status, ok.exit_code, ok.line_hash) == ("ok", 0, None)
    bad_data = fixture("post_tool_use_failure_bash.json", tmp_path)
    bad = check_hooks.outcome(bad_data)
    assert (bad.status, bad.exit_code) == ("fail", 1)
    # the signature's detail: a hash of the normalised last error line (the result count after it is skipped), the
    # same one check verify computes
    assert bad.line_hash == last_line_hash("FAILED tests/test_impl.py::test_fixed - AssertionError: took 9.99s") \
        == check_hooks.rough_signature(bad_data["error"])
    assert check_hooks.outcome({**bad_data, "is_interrupt": True}).status is None
    assert check_hooks.outcome({**bad_data, "error": "spawn /bin/sh ENOENT"}).status is None
    timed = check_hooks.outcome({**bad_data, "error": "Exit code 143\nCommand timed out after 2m 0s"})
    assert timed.status == "timeout"
    bg = fixture("post_tool_use_bash.json", tmp_path)
    bg["tool_input"]["run_in_background"] = True
    assert check_hooks.outcome(bg).status is None


# ------------------------------------------------------------------------------------------- the Bash hook

def test_declared_check_runs_are_recorded_by_the_hook(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path)
    cmd = f"{PY} check_ok.py"
    p = hook("bash-result", fixture("post_tool_use_bash.json", gate_repo, cmd), env, gate_repo)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    p = hook("bash-result", fixture("post_tool_use_failure_bash.json", gate_repo, "true"), env, gate_repo)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    rows = wait_attempts(lg, 2)
    by_check = {r["config"]["checks"][0]: r for r in rows}
    head = git(gate_repo, "rev-parse", "HEAD")
    ok, bad = by_check["unit"], by_check["quick"]
    assert ok["code_ref"] == head and ok["author"] == "hook" and ok["config"]["piece"] == "media"
    assert ok["duration_ms"] == 1234 and "tool_use_id toolu_01ABC123" in ok["notes"]
    assert ok["metrics"] == {"checks_passed": 1, "checks_total": 1, "checks": {"unit": "ok"},
                             "checks_declared": ["unit", "quick"], "failure_signature": None, "guard_touched": False,
                             "guard_files": [], "guard_weakened": False, "checks_changed": False, "via": "bash-hook",
                             "worktree_dirty": False,
                             "check_definitions": {"unit": config.load(gate_repo).checks["unit"].definition},
                             "run_by": "benchbook-worker"}
    assert bad["metrics"]["checks"] == {"quick": "fail"}
    line = "FAILED tests/test_impl.py::test_fixed - AssertionError: took 0.42s"
    assert bad["metrics"]["failure_signature"] == f"quick:exit 1:{last_line_hash(line)}"
    assert "SECRET-OUTPUT" not in json.dumps(rows, default=str)
    assert {o["kind"] for o in observed(tmp_path)} == {"direct"} and len(observed(tmp_path)) == 2


def test_other_commands_wrapped_and_piped_runs_are_not_recorded(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path)
    for cmd in ("ls -la", "git status", f"{PY} check_ok.py -v"):
        p = hook("bash-result", fixture("post_tool_use_bash.json", gate_repo, cmd), env, gate_repo)
        assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    assert not (tmp_path / "state").exists()  # nothing at all: no spool, no observation, no log line
    for cmd in ("benchbook check run unit", f"{PY} check_ok.py | tail -2"):
        assert hook("bash-result", fixture("post_tool_use_bash.json", gate_repo, cmd), env, gate_repo).returncode == 0
    assert [(o["check"], o["kind"]) for o in observed(tmp_path)] == [("unit", "wrapped"), ("unit", "unrecordable")]
    assert not list((tmp_path / "state").glob("benchbook/spool/*/*.json"))
    time.sleep(0.5)
    assert attempts(lg) == []


def test_a_check_run_from_a_subdirectory_is_not_recorded(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    """A declared check runs from the repository root; the same words in another directory test something else."""
    sub = gate_repo / "tests"
    hook("bash-result", fixture("post_tool_use_bash.json", sub, "true"), cli_env(tmp_path), sub)
    [line] = observed(tmp_path)
    assert (line["check"], line["kind"]) == ("quick", "unrecordable") and "repository root" in line["reason"]
    assert not list((tmp_path / "state").glob("benchbook/spool/*/*.json"))


def test_the_bash_hook_never_loads_the_database_layer(gate_repo: Path, tmp_path: Path) -> None:
    """Also on a match: it writes the spool and starts the processor, which does the database work elsewhere."""
    data = fixture("post_tool_use_bash.json", gate_repo, "true")
    code = ("import io, json, sys\n"
            "sys.argv = ['benchbook', 'hook', 'bash-result']\n"
            f"sys.stdin = io.StringIO({json.dumps(json.dumps(data))})\n"
            "from benchbook.entry import main\n"
            "try:\n    main()\nexcept SystemExit as e:\n    assert e.code == 0, e.code\n"
            "heavy = [m for m in sys.modules if m.split('.')[0] in ('typer', 'click', 'psycopg', 'jsonschema', "
            "'pydantic') or m.startswith(('benchbook.cli', 'benchbook.ledger', 'benchbook.db', 'benchbook.checks'))]\n"
            "assert not heavy, heavy\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=gate_repo, env=cli_env(tmp_path), capture_output=True,
                       text=True, timeout=60)
    assert (p.returncode, p.stderr) == (0, ""), p.stderr
    assert len(list((tmp_path / "state").glob("benchbook/spool/*/*.json"))) <= 1  # queued (or already drained)
    assert [o["kind"] for o in observed(tmp_path)] == ["direct"]


# ------------------------------------------------------------------------------------------- the completion gate

def test_completion_is_blocked_without_a_passing_run_and_names_the_command(gate_repo: Path, tmp_path: Path,
                                                                           lg: Ledger) -> None:
    p = hook("task-completed", fixture("task_completed.json", gate_repo), cli_env(tmp_path), gate_repo)
    assert p.returncode == 2 and p.stdout == ""
    head = git(gate_repo, "rev-parse", "HEAD")
    assert f"piece media has no passing run at HEAD {head[:12]} of: unit (not run), quick (not run)" in p.stderr
    assert "benchbook check run unit --piece media && benchbook check run quick --piece media" in p.stderr
    assert "task-completed: blocked piece media" in hook_log(tmp_path)


def test_a_piece_with_passing_runs_at_head_is_not_blocked(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path)
    task = fixture("task_completed.json", gate_repo)
    for name in ("unit", "quick"):
        assert subprocess.run([SCRIPT, "check", "run", name], cwd=gate_repo, env=env, capture_output=True,
                              timeout=60).returncode == 0
    wait_attempts(lg, 2)
    p = hook("task-completed", task, env, gate_repo)
    assert (p.returncode, p.stderr) == (0, "")
    assert "allowed: piece media passes every declared check" in hook_log(tmp_path)
    # a new commit is not covered by the runs of the old one
    (gate_repo / "impl.txt").write_text("v2\n")
    git(gate_repo, "commit", "-qam", "v2")
    assert hook("task-completed", task, env, gate_repo).returncode == 2


def test_failed_and_dirty_runs_do_not_open_the_gate(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path)
    task = fixture("task_completed.json", gate_repo)
    hook("bash-result", fixture("post_tool_use_failure_bash.json", gate_repo, "true"), env, gate_repo)
    wait_attempts(lg, 1)
    (gate_repo / "scratch.txt").write_text("not committed\n")
    assert subprocess.run([SCRIPT, "check", "run", "unit"], cwd=gate_repo, env=env, capture_output=True,
                          timeout=60).returncode == 0
    wait_attempts(lg, 2)
    p = hook("task-completed", task, env, gate_repo)
    assert p.returncode == 2
    assert "unit (not run), quick (its run failed)" in p.stderr and "1 uncommitted path(s)" in p.stderr


def test_a_run_still_in_the_spool_counts(gate_repo: Path, tmp_path: Path, lg: Ledger,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """check run returns before its processor has written the attempt; completing right after must not be blocked."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    spool = hooks._spool_dir()
    spool.mkdir(parents=True)
    head = git(gate_repo, "rev-parse", "HEAD")
    for name in ("unit", "quick"):
        hooks._write_spool(spool, {"event": "check-run", "project": lg.project_id, "accepted": True, "data": {
            "piece": "media", "check": name, "status": "ok", "sha": head, "dirty": False}})
    p = hook("task-completed", fixture("task_completed.json", gate_repo), cli_env(tmp_path), gate_repo)
    assert (p.returncode, p.stderr) == (0, "")


def test_recorded_without_an_agent_command(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    """Acceptance (spec §8): the agent runs the declared commands as plain Bash calls and nothing else; the Bash
    hook records them and the gate lets the completion through."""
    env = cli_env(tmp_path)
    task = fixture("task_completed.json", gate_repo)
    assert hook("task-completed", task, env, gate_repo).returncode == 2
    for cmd in (f"{PY} check_ok.py", "true"):
        hook("bash-result", fixture("post_tool_use_bash.json", gate_repo, cmd), env, gate_repo)
    assert len(wait_attempts(lg, 2)) == 2
    assert hook("task-completed", task, env, gate_repo).returncode == 0


def test_a_feature_branch_is_gated_only_while_it_is_a_live_piece(gate_repo: Path, tmp_path: Path, db_url: str,
                                                                  lg: Ledger) -> None:
    """PR #4 issue 1: where nothing is committed to main, the lead works on a feature branch too, and its own task
    list is not a piece. The branch is one once `piece start` names it, or BENCHBOOK_PIECE does."""
    env = cli_env(tmp_path)
    git(gate_repo, "checkout", "-q", "-b", "plan-branch")
    git(gate_repo, "commit", "-q", "--allow-empty", "-m", "chore: empty")
    task = fixture("task_completed.json", gate_repo)
    p = hook("task-completed", task, env, gate_repo)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    assert hook_log(tmp_path).count("task-completed: allowed: plan-branch has no live piece") == 1

    p = hook("task-completed", task, {**env, "BENCHBOOK_PIECE": "plan-branch"}, gate_repo)
    assert p.returncode == 2 and "piece plan-branch has no passing run" in p.stderr

    p = subprocess.run([SCRIPT, "--agent", "piece-lead", "piece", "start", "plan-branch", "--paths", "check_ok.py"],
                       cwd=gate_repo, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    p = hook("task-completed", task, env, gate_repo)
    head = git(gate_repo, "rev-parse", "HEAD")
    assert p.returncode == 2
    assert f"piece plan-branch has no passing run at HEAD {head[:12]} of: unit (not run), quick (not run)" in p.stderr
    assert "benchbook check run unit --piece plan-branch" in p.stderr

    pieces.done(_starter(db_url, lg.project_id), "plan-branch")
    assert hook("task-completed", task, env, gate_repo).returncode == 0  # released: no longer a piece


@pytest.mark.parametrize("case", ["no-project", "no-checks", "detached", "base-branch", "unreachable", "broken"])
def test_the_gate_fails_open(case: str, gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path)
    cwd = gate_repo
    if case == "no-project":
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
    elif case == "no-checks":
        (gate_repo / ".benchbook" / "config.toml").write_text(f'project = "{lg.project_id}"\n')
    elif case == "detached":
        git(gate_repo, "checkout", "-q", "--detach")
    elif case == "base-branch":
        git(gate_repo, "checkout", "-q", "main")
    elif case == "unreachable":
        env["BENCHBOOK_DATABASE_URL"] = "postgresql://benchbook@127.0.0.1:9/none?connect_timeout=1"
    elif case == "broken":
        (gate_repo / ".benchbook" / "config.toml").write_text("project = [not toml\n")
    p = hook("task-completed", fixture("task_completed.json", cwd), env, cwd)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    want = {"no-project": "allowed: no benchbook project", "no-checks": "allowed: no declared checks",
            "detached": "allowed: no piece", "base-branch": "allowed: " + str(gate_repo.resolve()) + " is on the base",
            "unreachable": "allowed without a check (gate error: OperationalError",
            "broken": "allowed without a check (gate error: TOMLDecodeError"}[case]
    assert want in hook_log(tmp_path), hook_log(tmp_path)


# ------------------------------------------------------------------------------------------- record coverage

def test_coverage_counts_from_the_documented_query(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    env = cli_env(tmp_path)
    for cmd in (f"{PY} check_ok.py", f"{PY} check_ok.py | tail -1", "benchbook check run quick"):
        hook("bash-result", fixture("post_tool_use_bash.json", gate_repo, cmd), env, gate_repo)
    subprocess.run([SCRIPT, "check", "run", "quick"], cwd=gate_repo, env=env, capture_output=True, timeout=60)
    wait_attempts(lg, 2)
    lines = observed(tmp_path)
    assert [o["kind"] for o in lines] == ["direct", "unrecordable", "wrapped"]
    assert all(o["project"] == lg.project_id for o in lines)
    doc = (ROOT / "docs" / "code.md").read_text()
    section = doc[doc.index("## Record coverage"):]
    [query] = re.findall(r'benchbook sql "(.*?)"', section, re.S)
    rows = lg.sql(query.replace("<since>", "2000-01-01"))["rows"]
    assert {r["via"]: r["attempts"] for r in rows} == {"bash-hook": 1, "check-run": 1}


# ------------------------------------------------------------------------------------------- wiring

def test_plugin_and_manual_hooks_run_both_events() -> None:
    plugin = ROOT / "integrations" / "claude-code"
    files = [plugin / "hooks" / "hooks.json", plugin / "manual" / "settings.hooks.json"]
    def plain(h: Any) -> Any:  # the plugin runs each hook through its shim (T07); the manual file runs benchbook
        return json.loads(json.dumps(h).replace('\\"${CLAUDE_PLUGIN_ROOT}/bin/benchbook-hook\\" ', "benchbook hook "))

    for f in files:
        h = plain(json.loads(f.read_text())["hooks"])
        # PreToolUse runs for git merge only (the merge gate): a hook on every Bash call would cost its start-up
        [gate] = h["PreToolUse"]
        assert gate["matcher"] == "Bash" and [(x["command"], x["if"]) for x in gate["hooks"]] == [
            ("benchbook hook bash-start", "Bash(git merge *)")]
        bash = [e for e in h["PostToolUse"] if e.get("matcher") == "Bash"]
        assert [x["command"] for e in bash for x in e["hooks"]] == ["benchbook hook bash-result"], f
        assert [x["command"] for e in h["PostToolUseFailure"] for x in e["hooks"]] == ["benchbook hook bash-result"]
        assert h["PostToolUseFailure"][0]["matcher"] == "Bash"
        [done] = h["TaskCompleted"]
        assert "matcher" not in done and [x["command"] for x in done["hooks"]] == ["benchbook hook task-completed"]
    assert plain(json.loads(files[0].read_text())) == json.loads(files[1].read_text())
    version = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert tuple(int(x) for x in version.split(".")) >= (0, 1, 3)


# The plugin's files as of its version. Claude Code keeps an installed version's cached files, so a change under
# integrations/claude-code/ reaches users only with a new version in plugin.json: change a file, and this fails until
# the version is bumped and the pair updated (docs/claude-code.md, "Bump version").
PLUGIN_RELEASE = ("0.2.2", "2a42ac21e6ff1f1c9f3e6745a839c6cbcb668acee78c22a020887913f5ed6fc6")


def test_the_plugin_version_moves_with_its_files() -> None:
    import hashlib

    root = Path(__file__).resolve().parent.parent / "integrations" / "claude-code"
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file() and "__pycache__" not in x.parts
                    and x.relative_to(root).as_posix() != ".claude-plugin/plugin.json"):
        h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    version = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert (version, h.hexdigest()) == PLUGIN_RELEASE, (
        "integrations/claude-code/ changed: bump its plugin.json version and update PLUGIN_RELEASE")


def test_the_cli_spelling_of_the_hooks_answers_the_same(gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    p = subprocess.run([sys.executable, "-m", "benchbook.cli", "hook", "task-completed"], cwd=gate_repo,
                       input=json.dumps(fixture("task_completed.json", gate_repo)), text=True, capture_output=True,
                       env=cli_env(tmp_path), timeout=60)
    assert p.returncode == 2 and "benchbook check run unit --piece media" in p.stderr
    assert os.environ.get("BENCHBOOK_DATABASE_URL")
