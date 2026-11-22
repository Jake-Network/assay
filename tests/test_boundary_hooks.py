"""The boundary hooks (T09): session-start, pre-compact and notes (docs/claude-code.md, "The boundary hooks")."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook import boundary_hooks, harnesses
from benchbook.entry import hook_args
from benchbook.ids import short
from benchbook.verify_requests import reported_ref
from tests.conftest import CONFIG, make_project, make_repo, piece
from tests.test_code_brief import _finding, agent, attempt, git

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "tests" / "fixtures" / "hooks"
SHIM = '"${CLAUDE_PLUGIN_ROOT}/bin/benchbook-hook" '


def fixture(name: str, cwd: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FX / name).read_text())
    data["cwd"] = str(cwd)
    return data


def run(event: str, data: dict[str, Any], capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch, harness: str | None = None) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    boundary_hooks.main(event, harness)
    out, err = capsys.readouterr()
    assert err == ""
    return out


def context(out: str, event: str = "SessionStart") -> str:
    answer = json.loads(out)
    assert answer["hookSpecificOutput"]["hookEventName"] == event
    text = answer["hookSpecificOutput"]["additionalContext"]
    assert isinstance(text, str)
    return text


@pytest.fixture
def env(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", db_url)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for name in ("BENCHBOOK_PROJECT", "BENCHBOOK_TOKEN", "BENCHBOOK_AGENT", "ASSAY_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(boundary_hooks, "abandoned", False)
    return tmp_path


def code_repo(root: Path, project: str) -> Path:
    """A checkout of a code project whose .benchbook/config.toml is committed, on branch media-upload, which changed
    media/."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(root, "config", k, v)
    (root / ".benchbook").mkdir()
    (root / ".benchbook" / "config.toml").write_text(f'project = "{project}"\n\n[guard]\nbase = "main"\n')
    (root / "media").mkdir()
    (root / "media" / "upload.py").write_text("v1\n")
    (root / "auth.py").write_text("v1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "media-upload")
    (root / "media" / "upload.py").write_text("v2\n")
    git(root, "commit", "-qam", "media")
    return root


@pytest.fixture
def code(db_url: str) -> str:
    from benchbook.packs import load_pack

    d = load_pack("code").project_defaults
    return make_project(db_url, metric=d["metric"], direction=d["direction"], pack="code",
                        settings=dict(d["settings"]))


@pytest.fixture
def recorded(db_url: str, code: str, env: Path) -> dict[str, Any]:
    repo = code_repo(env / "repo", code)
    a = agent(db_url, code, "impl-a")
    d = attempt(a, "media-upload", approach="hash-before-put", failed=[["tests/test_media.py::t", "AssertionError"]])
    f = _finding(a, "Logout races with token refresh.", ["mod:auth"])  # not in this branch's scope
    return {"repo": repo, "discarded": d.discarded_id, "finding": f}


# ------------------------------------------------------------------------------------------- session-start

def test_session_start_gives_the_branch_brief_and_writes_state(recorded: dict[str, Any], env: Path,
                                                               capsys: pytest.CaptureFixture[str],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    repo = recorded["repo"]
    for name in ("session_start_startup.json", "session_start_compact.json"):
        text = context(run("session-start", fixture(name, repo), capsys, monkeypatch))
        assert text.startswith("# benchbook brief") and "Scope: piece:media-upload, file:media/" in text
        assert short("claim", recorded["discarded"]) in text and short("claim", recorded["finding"]) not in text
        assert len(text) <= boundary_hooks.MAX_CHARS
    state = (repo / ".benchbook" / "STATE.md").read_text()
    first, blank, rest = state.split("\n", 2)
    head = git(repo, "rev-parse", "--short=12", "HEAD")
    assert first.startswith("benchbook state · written ") and f"HEAD {head}" in first and blank == ""
    assert rest == text
    assert git(repo, "status", "--porcelain") == ""  # STATE.md is kept out of git status
    assert git(repo, "check-ignore", ".benchbook/STATE.md") == ".benchbook/STATE.md"


def test_an_empty_ledger_gives_nothing_but_writes_state(db_url: str, code: str, env: Path,
                                                        capsys: pytest.CaptureFixture[str],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo = code_repo(env / "repo", code)
    assert run("session-start", fixture("session_start_startup.json", repo), capsys, monkeypatch) == ""
    assert run("pre-compact", fixture("pre_compact.json", repo), capsys, monkeypatch) == ""
    assert "- none recorded" in (repo / ".benchbook" / "STATE.md").read_text()


def test_a_silent_ledger_gives_the_last_state(recorded: dict[str, Any], env: Path,
                                              capsys: pytest.CaptureFixture[str],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    repo = recorded["repo"]
    data = fixture("session_start_startup.json", repo)
    before = context(run("session-start", data, capsys, monkeypatch))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://benchbook@127.0.0.1:1/none?connect_timeout=1")
    down = context(run("session-start", data, capsys, monkeypatch))
    assert down.startswith("benchbook: the ledger did not answer") and before in down
    assert len(down) <= boundary_hooks.MAX_CHARS
    assert run("pre-compact", fixture("pre_compact.json", repo), capsys, monkeypatch) == ""

    def slow(w: Any) -> str:
        time.sleep(3)
        return "late"

    monkeypatch.setattr(boundary_hooks, "compile_brief", slow)
    monkeypatch.setattr(boundary_hooks, "TIMEOUT", 0.2)
    t = time.monotonic()
    late = context(run("session-start", data, capsys, monkeypatch))
    assert time.monotonic() - t < 2 and late.startswith("benchbook: the ledger did not answer (no answer within 0.2 s)")
    assert boundary_hooks.abandoned
    (repo / ".benchbook" / "STATE.md").unlink()
    assert run("session-start", data, capsys, monkeypatch) == ""  # nothing to fall back on


def test_the_brief_stays_within_six_thousand_characters(db_url: str, code: str, env: Path,
                                                        capsys: pytest.CaptureFixture[str],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo = code_repo(env / "repo", code)
    git(repo, "checkout", "-q", "main")  # the whole project
    lg = agent(db_url, code, "impl-a")
    with lg._conn() as conn:
        conn.execute("""
INSERT INTO benchbook.claims (project_id, kind, statement, statement_hash, payload, tags, author)
SELECT %(p)s, 'observation', left('finding ' || i || ' ' || repeat('a long out of scope finding ', 20), 500),
       md5('t09:' || i), '{}'::jsonb, ARRAY['out-of-scope', 'file:media/upload.py'], 'impl-a'
FROM generate_series(1, 2000) AS i""", {"p": code})
    text = context(run("session-start", fixture("session_start_startup.json", repo), capsys, monkeypatch))
    assert len(text) <= 6000 and "## Open out-of-scope findings (" in text


# ------------------------------------------------------------------------------------------- pre-compact

def test_pre_compact_gives_one_paragraph_of_ids(recorded: dict[str, Any], env: Path,
                                                capsys: pytest.CaptureFixture[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    out = run("pre-compact", fixture("pre_compact.json", recorded["repo"]), capsys, monkeypatch)
    assert out.endswith("\n") and "\n" not in out[:-1] and len(out) <= 1001
    assert out.startswith(boundary_hooks.PRECOMPACT_HEAD) and short("claim", recorded["discarded"]) in out
    assert (recorded["repo"] / ".benchbook" / "STATE.md").is_file()


def test_compact_paragraph_keeps_whole_items_in_order() -> None:
    def section(title: str, n: int) -> str:
        return f"## {title} ({n} of {n})\n" + "".join(f"- {title.lower()}-{i} c:{i:08x}\n" for i in range(n))

    brief = ("# benchbook brief\nScope: x\n\n" + section("Verified", 5) + "\n" + section("Reported", 0)
             + "- none recorded\n\n" + section("Discarded", 2) + "\nDetails: benchbook get <id>\n")
    p = boundary_hooks.compact_paragraph(brief)
    assert p == (boundary_hooks.PRECOMPACT_HEAD + "Verified: verified-0 c:00000000; verified-1 c:00000001; "
                 "verified-2 c:00000002. Discarded: discarded-0 c:00000000; discarded-1 c:00000001.")
    short_p = boundary_hooks.compact_paragraph(brief, limit=len(boundary_hooks.PRECOMPACT_HEAD) + 60)
    assert short_p.endswith("verified-1 c:00000001.") and len(short_p) <= len(boundary_hooks.PRECOMPACT_HEAD) + 60
    tiny = boundary_hooks.compact_paragraph(brief, limit=40)
    assert len(tiny) == 40 and tiny.endswith("…")
    assert boundary_hooks.compact_paragraph("# benchbook brief\n\n## A (0 of 0)\n- none recorded\n") == ""


# ------------------------------------------------------------------------------------------- notes

def test_notes_are_given_once(env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    data = fixture("user_prompt_submit.json", env)
    notes = env / "state" / "benchbook" / "notes" / data["session_id"]
    notes.mkdir(parents=True)
    (notes / "001-verify.md").write_text("benchbook re-ran media-upload at abc123: verified (c:1234abcd).\n")
    (notes / "002-verify.md").write_text("benchbook re-ran auth at def456: refuted (c:5678abcd).\n")
    (notes / "ignored.json").write_text("{}")
    text = context(run("notes", data, capsys, monkeypatch), "UserPromptSubmit")
    assert text == ("benchbook re-ran media-upload at abc123: verified (c:1234abcd).\n\n"
                    "benchbook re-ran auth at def456: refuted (c:5678abcd).")
    assert sorted(p.name for p in (notes / "delivered").iterdir()) == ["001-verify.md", "002-verify.md"]
    assert run("notes", data, capsys, monkeypatch) == ""
    assert run("notes", {**data, "session_id": "../../etc"}, capsys, monkeypatch) == ""


# ------------------------------------------------------------------------------------------- fast path and wiring

@pytest.mark.parametrize(("event", "name"), [("session-start", "session_start_startup.json"),
                                             ("pre-compact", "pre_compact.json"),
                                             ("agent-result", "post_tool_use_agent_async.json"),
                                             ("notes", "user_prompt_submit.json")])
def test_outside_a_project_the_hook_leaves_within_50ms(event: str, name: str, tmp_path: Path) -> None:
    """Timed like the Bash hook's acceptance test: from the console script's entry point to its exit, in a fresh
    interpreter, in a directory with no benchbook project. Nothing is printed and nothing is written."""
    data = fixture(name, tmp_path)
    code = ("import io, sys, time\n"
            f"sys.argv = {['benchbook', 'hook', event]!r}\n"
            "sys.stdin = io.StringIO(" + repr(json.dumps(data)) + ")\n"
            "t = time.perf_counter()\n"
            "from benchbook.entry import main\n"
            "try:\n    main()\nexcept SystemExit as e:\n    assert e.code == 0, e.code\n"
            "print(time.perf_counter() - t)\n"
            "assert 'psycopg' not in sys.modules and 'typer' not in sys.modules, sorted(sys.modules)\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BENCHBOOK_", "ASSAY_"))}
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    times = []
    for _ in range(5):
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, env=env,
                           timeout=60)
        assert p.returncode == 0 and p.stderr == "", p.stderr
        [elapsed] = p.stdout.splitlines()
        times.append(float(elapsed))
    assert min(times) < 0.05, f"{event} took {min(times) * 1000:.1f} ms"
    assert not (tmp_path / "state").exists() and not (tmp_path / ".benchbook").exists()


def test_the_plugin_runs_the_boundary_hooks() -> None:
    plugin = ROOT / "integrations" / "claude-code"
    want = {"SessionStart": "session-start", "PreCompact": "pre-compact", "UserPromptSubmit": "notes"}
    for f, prefix in ((plugin / "hooks" / "hooks.json", SHIM), (plugin / "manual" / "settings.hooks.json",
                                                                 "benchbook hook ")):
        h = json.loads(f.read_text())["hooks"]
        for name, event in want.items():
            [entry] = h[name]
            assert "matcher" not in entry and [x["command"] for x in entry["hooks"]] == [prefix + event], f
            assert hook_args(["hook", event]) == (event, None)
        [agent_hook] = [entry for entry in h["PostToolUse"] if entry["matcher"] == "Agent|Task"]
        assert [x["command"] for x in agent_hook["hooks"]] == [prefix + "agent-result"]
        assert hook_args(["hook", "agent-result"]) == ("agent-result", None)


def test_context_answer_per_harness() -> None:
    ss = json.loads(harnesses.context_answer("claude-code", "session-start", "brief"))
    assert ss == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "brief"}}
    ups = json.loads(harnesses.context_answer("claude-code", "notes", "n"))
    assert ups["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert harnesses.context_answer("claude-code", "pre-compact", "keep c:1") == "keep c:1\n"
    assert harnesses.context_answer("claude-code", "session-start", "") == ""


def test_report_commit_priority() -> None:
    sha = "a" * 40
    assert reported_ref(f"commit: {sha}\nDONE abcdef1") == "abcdef1"
    assert reported_ref(f"Investigated {sha}\n커밋: abcdef2") == "abcdef2"
    assert reported_ref(f"Finished {sha}, previously {sha}.") == sha
    assert reported_ref(f"Compared {sha} to {'b' * 40}.") is None


@pytest.mark.parametrize(("case", "expected"), [
    ("pass", "re-run passed (2 checks"),
    ("weak", "tests weakened"),
    ("environment", "environment failure"),
])
def test_agent_report_replays_and_delivers_once(case: str, expected: str, code: str, env: Path, db_url: str,
                                                capsys: pytest.CaptureFixture[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from benchbook import check_hooks, hooks

    cfg = CONFIG.replace('project = "ignored"', f'project = "{code}"')
    if case == "environment":
        cfg += '\n[verify]\nprepare = "false"\n'
    repo = make_repo(env / "repo", cfg)
    changes = {"impl.txt": "fixed\n"}
    if case == "weak":
        changes["tests/test_extra.py"] = "def test_extra():\n    assert 1 == 1\n"
    sha = piece(repo, "media-upload", changes)
    data = fixture("post_tool_use_agent.json", repo)
    data["tool_response"]["content"][0]["text"] = f"private report text\npiece: media-upload\nDONE {sha[:9]}"
    assert not run("agent-result", fixture("post_tool_use_agent_async.json", repo), capsys, monkeypatch)
    monkeypatch.setattr(hooks, "start_processor", lambda spool: None)
    for _ in range(2):
        out = run("agent-result", data, capsys, monkeypatch)
        assert "re-running the checks of media-upload" in context(out, "PostToolUse")
    spool = hooks._spool_dir()
    assert len(list(spool.glob("*.json"))) == 2
    assert all("private report text" not in path.read_text() for path in spool.glob("*.json"))
    assert hooks.replay_now(spool).recorded == 2
    lg = agent(db_url, code, "lead")
    [attempt] = lg.sql_internal("SELECT id FROM benchbook.experiments WHERE project_id=%s AND code_ref=%s",
                                (code, sha))
    [claim] = lg.sql_internal("SELECT id FROM benchbook.claims WHERE project_id=%s AND kind='completion'",
                              (code,))
    assert attempt["id"]
    completion = lg.completion_attempts(claim["id"])
    assert completion is not None
    assert (completion["status"] == "verified") == (case == "pass")
    bash = {"session_id": data["session_id"], "cwd": str(repo), "tool_name": "Bash",
            "tool_input": {"command": "true"}, "hook_event_name": "PostToolUse"}
    for delivery in range(2):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bash)))
        assert check_hooks.bash_result() == 0
        out, err = capsys.readouterr()
        assert not err
        if delivery == 0:
            result = context(out, "PostToolUse")
            assert expected in result and short("claim", claim["id"]) in result
        else:
            assert not out
