"""The boundary hooks (T09): session-start, pre-compact and notes (docs/claude-code.md, "The boundary hooks")."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest

from benchbook import boundary_hooks, harnesses
from benchbook.core.ids import short
from benchbook.entry import hook_args
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


def test_a_connection_refusal_gives_the_last_state(recorded: dict[str, Any], env: Path,
                                                   capsys: pytest.CaptureFixture[str],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    repo = recorded["repo"]
    data = fixture("session_start_startup.json", repo)
    before = context(run("session-start", data, capsys, monkeypatch))

    def refused(w: Any) -> str:  # the same ledger, down: its server refuses connections
        raise psycopg.OperationalError("connection failed: connect: Connection refused")

    monkeypatch.setattr(boundary_hooks, "compile_brief", refused)
    down = context(run("session-start", data, capsys, monkeypatch))
    assert down.startswith("benchbook: the ledger did not answer (unreachable") and before in down
    assert len(down) <= boundary_hooks.MAX_CHARS
    assert run("pre-compact", fixture("pre_compact.json", repo), capsys, monkeypatch) == ""


@pytest.mark.parametrize("remote", [False, True])
def test_only_an_embedded_timeout_gives_the_last_state(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                       monkeypatch: pytest.MonkeyPatch, remote: bool) -> None:
    git(tmp_path, "init", "-q")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_PROJECT", "app")
    for name in ("BENCHBOOK_DATABASE_URL", "ASSAY_DATABASE_URL", "BENCHBOOK_TOKEN", "ASSAY_TOKEN", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)
    if remote:
        monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://reviewer@127.0.0.1:5432/app")
    else:
        monkeypatch.setenv("BENCHBOOK_DATA_DIR", str(tmp_path / "pg"))
    data = fixture("session_start_startup.json", tmp_path)
    monkeypatch.setattr(boundary_hooks, "compile_brief", lambda w: "# brief\n- a finding")
    assert context(run("session-start", data, capsys, monkeypatch)) == "# brief\n- a finding"

    def slow(w: Any) -> str:
        time.sleep(3)
        return "late"

    monkeypatch.setattr(boundary_hooks, "compile_brief", slow)
    monkeypatch.setattr(boundary_hooks, "TIMEOUT", 0.2)
    t = time.monotonic()
    late = run("session-start", data, capsys, monkeypatch)
    assert time.monotonic() - t < 2
    if remote:
        assert late == ""
    else:
        assert context(late).startswith("benchbook: the ledger did not answer (no answer within 0.2 s)")
    assert boundary_hooks.abandoned
    monkeypatch.setattr(boundary_hooks, "abandoned", False)
    (tmp_path / ".benchbook" / "STATE.md").unlink()
    assert run("session-start", data, capsys, monkeypatch) == ""  # nothing to fall back on


@pytest.mark.parametrize("change", ["another ledger", "another project", "token refused", "login refused"])
def test_the_last_state_is_given_only_for_this_project_on_this_ledger_when_it_is_down(
        recorded: dict[str, Any], env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
        change: str) -> None:
    """STATE.md is per checkout. A brief written for another project or from another database never stands in, and a
    ledger that answered with a refusal is not down: nothing is given."""
    from benchbook.errors import InvalidAgentToken

    repo = recorded["repo"]
    data = fixture("session_start_startup.json", repo)
    assert context(run("session-start", data, capsys, monkeypatch))
    if change == "another ledger":  # unreachable, but not the database STATE.md came from
        monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://benchbook@127.0.0.1:1/none?connect_timeout=1")
    elif change == "another project":  # same checkout, switched to a project that is down or unknown
        other = make_project(os.environ["BENCHBOOK_DATABASE_URL"])
        cfg = repo / ".benchbook" / "config.toml"
        cfg.write_text(cfg.read_text().replace(f'"{recorded_project(repo)}"', f'"{other}"'))

        def down(w: Any) -> str:
            raise psycopg.OperationalError("connection failed: connect: Connection refused")

        monkeypatch.setattr(boundary_hooks, "compile_brief", down)
    else:
        def refuse(w: Any) -> str:
            if change == "token refused":
                raise InvalidAgentToken("BENCHBOOK_TOKEN is not valid for project x")
            raise psycopg.OperationalError('connection failed: FATAL:  password authentication failed for user "b"')

        monkeypatch.setattr(boundary_hooks, "compile_brief", refuse)
    assert run("session-start", data, capsys, monkeypatch) == ""


def test_state_fallback_tracks_libpq_database_and_skips_tokens_and_services(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    git(tmp_path, "init", "-q")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BENCHBOOK_PROJECT", "app")
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://reviewer@127.0.0.1:5432")
    for name in ("BENCHBOOK_TOKEN", "ASSAY_TOKEN", "PGSERVICE", "PGHOSTADDR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PGDATABASE", "private_a")
    data = fixture("session_start_startup.json", tmp_path)
    brief = "# brief\n- private_a finding"
    monkeypatch.setattr(boundary_hooks, "compile_brief", lambda w: brief)
    assert context(run("session-start", data, capsys, monkeypatch)) == brief

    def down(w: Any) -> str:
        raise psycopg.OperationalError("connection failed: connect: Connection refused")

    monkeypatch.setattr(boundary_hooks, "compile_brief", down)
    monkeypatch.setenv("PGDATABASE", "private_b")
    assert run("session-start", data, capsys, monkeypatch) == ""
    monkeypatch.setenv("PGDATABASE", "private_a")
    assert brief in context(run("session-start", data, capsys, monkeypatch))

    with monkeypatch.context() as m:
        m.setenv("BENCHBOOK_TOKEN", "not-authenticated")
        assert run("session-start", data, capsys, m) == ""
    monkeypatch.setenv("PGSERVICE", "other")
    assert run("session-start", data, capsys, monkeypatch) == ""


def test_ipv6_host_and_port_do_not_collide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGSERVICE", raising=False)
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://reader@[2001:db8::1]:5432/benchbook")
    first = boundary_hooks.ledger_id()
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", "postgresql://reader@[2001:db8::1:5432]/benchbook")
    assert first and boundary_hooks.ledger_id() not in {None, first}


def recorded_project(repo: Path) -> str:
    import tomllib

    return str(tomllib.loads((repo / ".benchbook" / "config.toml").read_text())["project"])


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


def test_a_rerun_that_decided_nothing_is_in_every_brief_with_why(db_url: str, code: str, env: Path,
                                                                  capsys: pytest.CaptureFixture[str],
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary pilot: a piece whose re-run passed but whose commit changed a test was in no section of the
    brief, so a new session's lead could not know it waited for a person. It is listed after the verified pieces with
    the reason, in session start, pre-compact and brief --here; so is a re-run that failed. A verified piece is not."""
    from benchbook import Ledger, checks
    from tests.test_code_brief import SCRIPT
    from tests.test_mutations import FIXED, NO_ASSERT

    repo = make_repo(env / "repo", CONFIG.replace('project = "ignored"', f'project = "{code}"'))
    lead = Ledger.open(project=code, agent="lead", role="orchestrator", database_url=db_url)
    shas = {"weak": piece(repo, "weak", {**FIXED, "tests/test_impl.py": NO_ASSERT}),
            "good": piece(repo, "good", FIXED), "bad": piece(repo, "bad", {**FIXED, "value.txt": "no\n"})}
    ids = {}
    for name, sha in shas.items():
        _, rec = checks.verify(lead, name, sha, repo=repo, reported_by="impl")
        ids[name] = short("claim", rec.claim_id)
    weak = (f"- weak at {shas['weak'][:12]} ({ids['weak']}, re-run AGE: passed, needs review (tests weakened: "
            "tests/test_impl.py))")
    bad = f"- bad at {shas['bad'][:12]} ({ids['bad']}, re-run AGE: failed (value))"

    def aged(lines: list[str]) -> list[str]:  # the age moves with this machine's clock steps (54.6 s)
        return sorted(re.sub(r", re-run [^:]+:", ", re-run AGE:", line) for line in lines)

    text = context(run("session-start", fixture("session_start_startup.json", repo), capsys, monkeypatch))
    sections = text.split("\n## ")
    assert sections[1].startswith("Last verified commit per piece (1 of 1)\n- good at ")
    lines = sections[2].splitlines()
    assert lines[0] == "Re-run, not decided (2 of 2)" and aged(lines[1:]) == sorted([weak, bad])
    assert ids["good"] not in sections[2] and sections[3].startswith("Reported done, not re-run (0 of 0)")
    out = run("pre-compact", fixture("pre_compact.json", repo), capsys, monkeypatch)
    assert ids["weak"] in out and ids["bad"] in out

    git(repo, "checkout", "-q", "weak")
    cli_env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url, "XDG_STATE_HOME": str(env / "state")}
    p = subprocess.run([SCRIPT, "--agent", "reader", "brief", "--here"], capture_output=True, text=True, cwd=repo,
                       timeout=120, env=cli_env)
    assert p.returncode == 0, p.stderr
    assert weak in aged(p.stdout.splitlines()) and "## Re-run, not decided (" in p.stdout


# ------------------------------------------------------------------------------------------- pre-compact

def test_pre_compact_gives_one_paragraph_of_ids(recorded: dict[str, Any], env: Path,
                                                capsys: pytest.CaptureFixture[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    out = run("pre-compact", fixture("pre_compact.json", recorded["repo"]), capsys, monkeypatch)
    assert out.endswith("\n") and "\n" not in out[:-1] and len(out) <= 1001
    assert out.startswith(boundary_hooks.PRECOMPACT_HEAD) and short("claim", recorded["discarded"]) in out
    assert (recorded["repo"] / ".benchbook" / "STATE.md").is_file()


def test_pre_compact_marks_a_piece_not_merged_into_this_branch(db_url: str, code: str, env: Path,
                                                               capsys: pytest.CaptureFixture[str],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #4 issue 2: the paragraph keeps the brief's lines, so a piece verified on its own branch and read from the
    base says so instead of "changed since (0 commits)"."""
    from benchbook import pieces
    from tests.test_code_brief import _verify

    repo = code_repo(env / "repo", code)
    tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    pieces.start(agent(db_url, code, "impl-a"), "media-upload", ["media/upload.py"], repo)
    _verify(db_url, code, "media-upload", tip)
    out = run("pre-compact", fixture("pre_compact.json", repo), capsys, monkeypatch)
    assert f"media-upload at {tip[:12]}" in out and "not merged into this branch" in out
    assert "changed since" not in out


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


def _pending(env: Path, session: str, *lines: str) -> Path:
    notes = env / "state" / "benchbook" / "notes" / session
    notes.mkdir(parents=True, exist_ok=True)
    for i, line in enumerate(lines):
        (notes / f"verify-{i:03}.txt").write_text(line + "\n")
    return notes


def _tool_result(session: str, cwd: Path, tool: str = "Read", **extra: Any) -> dict[str, Any]:
    return {"session_id": session, "cwd": str(cwd), "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": {"file_path": "app.py"}, "tool_response": {}, **extra}


def test_notes_after_any_tool_answer_as_post_tool_use(env: Path, capsys: pytest.CaptureFixture[str],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead calling a tool whose own hook does not deliver (Read, Grep, an MCP tool) gets the verdict right
    after that tool's result, under the event that ran."""
    _pending(env, "s-lead", "benchbook: media-upload abc1234: re-run passed")
    assert context(run("notes", _tool_result("s-lead", env), capsys, monkeypatch), "PostToolUse") == (
        "benchbook: media-upload abc1234: re-run passed")
    assert run("notes", _tool_result("s-lead", env), capsys, monkeypatch) == ""


@pytest.mark.parametrize("ran", [["PostToolUse"], {"x": 1}, 7, None])
def test_a_malformed_event_name_still_delivers_the_notes(ran: object, env: Path, capsys: pytest.CaptureFixture[str],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook_event_name that is not a string must not fail the answer after the notes moved to delivered/: they
    would be lost. The answer falls back to the hook's registered event."""
    _pending(env, "s-odd", "benchbook: auth def5678: re-run passed")
    data = {**_tool_result("s-odd", env), "hook_event_name": ran}
    assert context(run("notes", data, capsys, monkeypatch), "UserPromptSubmit") == (
        "benchbook: auth def5678: re-run passed")


def test_a_subagents_tool_result_leaves_the_leads_notes(env: Path, capsys: pytest.CaptureFixture[str],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code gives a subagent's hooks the lead's session_id and the subagent's agent_id. A worker's Bash or
    Read result must not take the lead's verdict into the worker's context; the lead's next tool result gets it."""
    from benchbook import check_hooks

    notes = _pending(env, "s-swarm", "benchbook: media-upload abc1234: re-run failed")
    worker = {"agent_id": "a1b2c3", "agent_type": "general-purpose"}
    assert run("notes", _tool_result("s-swarm", env, **worker), capsys, monkeypatch) == ""
    bash = {**fixture("post_tool_use_bash.json", env), "session_id": "s-swarm"}
    bash["tool_input"]["command"] = "true"
    assert bash["agent_id"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bash)))
    assert check_hooks.bash_result() == 0
    assert capsys.readouterr() == ("", "")
    agent_call = {**fixture("post_tool_use_agent_async.json", env), "session_id": "s-swarm", **worker}
    assert run("agent-result", agent_call, capsys, monkeypatch) == ""
    assert [p.name for p in notes.iterdir()] == ["verify-000.txt"]
    lead_bash = {key: value for key, value in bash.items() if key not in ("agent_id", "agent_type")}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(lead_bash)))
    assert check_hooks.bash_result() == 0
    assert "re-run failed" in context(capsys.readouterr().out, "PostToolUse")
    assert not (notes / "verify-000.txt").exists()


def test_a_launched_background_agent_delivers_the_leads_notes(env: Path, capsys: pytest.CaptureFixture[str],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """A lead fanning work out calls Agent more than any other tool, and on Claude Code 2.1.330 every call comes back
    async_launched: that result queues nothing (there is no report yet) but hands over what is waiting."""
    _pending(env, "s-fan", "benchbook: auth def5678: re-run passed")
    launched = {**fixture("post_tool_use_agent_async.json", env), "session_id": "s-fan"}
    assert context(run("agent-result", launched, capsys, monkeypatch), "PostToolUse") == (
        "benchbook: auth def5678: re-run passed")
    assert run("agent-result", launched, capsys, monkeypatch) == ""


def test_hooks_running_at_once_deliver_a_note_once(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bash's hook, the notes hook and agent-result can run at the same moment in one session (parallel tool
    calls): each note goes to exactly one of them."""
    from concurrent.futures import ThreadPoolExecutor

    from benchbook import session_notes

    lines = [f"benchbook: piece-{i} {i:07}: re-run passed" for i in range(20)]
    _pending(env, "s-race", *lines)
    with ThreadPoolExecutor(8) as pool:
        given = list(pool.map(lambda _: session_notes.deliver("s-race"), range(16)))
    delivered = [line for text in given for line in text.split("\n\n") if line]
    assert sorted(delivered) == sorted(lines)


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
        # Every other tool's result delivers notes too; Bash and Agent deliver from their own hooks.
        [notes_hook] = [entry for entry in h["PostToolUse"] if [x["command"] for x in entry["hooks"]]
                        == [prefix + "notes"]]
        matches = {tool: re.search(notes_hook["matcher"], tool) is not None
                   for tool in ("Bash", "Agent", "Task", "Read", "Edit", "Grep", "BashOutput", "TaskCreate",
                                "mcp__plugin_benchbook-mcp_benchbook__brief")}
        assert matches == {"Bash": False, "Agent": False, "Task": False, "Read": True, "Edit": True, "Grep": True,
                           "BashOutput": True, "TaskCreate": True,
                           "mcp__plugin_benchbook-mcp_benchbook__brief": True}, f


def test_context_answer_per_harness() -> None:
    ss = json.loads(harnesses.context_answer("claude-code", "session-start", "brief"))
    assert ss == {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "brief"}}
    ups = json.loads(harnesses.context_answer("claude-code", "notes", "n"))
    assert ups["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    for ran in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure"):
        answer = json.loads(harnesses.context_answer("claude-code", "notes", "n", ran))
        assert answer["hookSpecificOutput"]["hookEventName"] == ran
    for odd in ("Stop", ["PostToolUse"], {"a": 1}):
        other = json.loads(harnesses.context_answer("claude-code", "notes", "n", odd))
        assert other["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    codex = json.loads(harnesses.context_answer("codex", "notes", "n", "UserPromptSubmit"))
    assert codex["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert harnesses.context_answer("claude-code", "pre-compact", "keep c:1") == "keep c:1\n"
    assert harnesses.context_answer("claude-code", "session-start", "") == ""
    gemini = json.loads(harnesses.context_answer("gemini", "notes", "n"))
    assert gemini == {"hookSpecificOutput": {"hookEventName": "AfterTool", "additionalContext": "n"}}
    assert json.loads(harnesses.context_answer("cursor", "session-start", "b")) == {"additional_context": "b"}


def test_report_commit_priority() -> None:
    sha = "a" * 40
    assert reported_ref(f"commit: {sha}\nDONE abcdef1") == "abcdef1"
    assert reported_ref(f"Investigated {sha}\n커밋: abcdef2") == "abcdef2"
    assert reported_ref(f"Finished {sha}, previously {sha}.") == sha
    assert reported_ref(f"Compared {sha} to {'b' * 40}.") is None


def test_a_background_agent_report_is_re_run_from_subagent_stop(code: str, env: Path, db_url: str,
                                                                capsys: pytest.CaptureFixture[str],
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code 2.1.330 launches every Agent call in the background (T08): the report comes with SubagentStop,
    which gives the lead no context. The queued line and the result wait as notes for the next hook that does."""
    from benchbook import hooks

    repo = make_repo(env / "repo", CONFIG.replace('project = "ignored"', f'project = "{code}"'))
    sha = piece(repo, "media-upload", {"impl.txt": "fixed\n"})
    monkeypatch.setattr(hooks, "start_processor", lambda *args: None)
    compaction = fixture("subagent_stop_compaction.json", repo)
    compaction["last_assistant_message"] = f"<summary>piece: media-upload\nDONE {sha}</summary>"
    assert not run("agent-result", compaction, capsys, monkeypatch)
    assert not list(hooks._spool_dir().glob("*.json"))
    stop = fixture("subagent_stop_background.json", repo)
    stop["last_assistant_message"] = f"Implemented.\nDONE {sha[:9]}"
    for _ in range(2):
        assert not run("agent-result", stop, capsys, monkeypatch)
    spool = hooks._spool_dir()
    [request] = [json.loads(p.read_text()) for p in spool.glob("*.json")][:1]
    assert request["event"] == "verify-request" and request["data"]["agent_type"] == "general-purpose"
    assert hooks.replay_now(spool).recorded == 2
    notification = fixture("user_prompt_submit_task_notification.json", repo)
    notification["session_id"] = stop["session_id"]
    given = context(run("notes", notification, capsys, monkeypatch), "UserPromptSubmit")
    lines = given.splitlines()
    assert lines[0] == f"benchbook: re-running the checks of media-upload at {sha[:7]} in the background"
    assert "re-run passed (2 checks" in given and len(lines) == 3  # queued, blank, result: each once
    assert not run("notes", notification, capsys, monkeypatch)


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
    monkeypatch.setattr(hooks, "start_processor", lambda *args: None)
    out = run("agent-result", data, capsys, monkeypatch)
    assert "re-running the checks of media-upload" in context(out, "PostToolUse")
    assert not run("agent-result", data, capsys, monkeypatch)  # the line once; the request again (a new report)
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


@pytest.mark.parametrize("stop_first", [True, False], ids=["subagent-stop-first", "post-tool-use-first"])
def test_a_foreground_report_seen_by_both_hooks_tells_the_lead_once(stop_first: bool, code: str, env: Path,
                                                                    capsys: pytest.CaptureFixture[str],
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary pilot (2.1.331): one report came as SubagentStop and as PostToolUse:Agent, and the lead got
    "re-running the checks" twice. Either order gives the line once; SubagentStop first queues the re-run once."""
    from benchbook import hooks

    repo = make_repo(env / "repo", CONFIG.replace('project = "ignored"', f'project = "{code}"'))
    sha = piece(repo, "media-upload", {"impl.txt": "fixed\n"})
    monkeypatch.setattr(hooks, "start_processor", lambda *args: None)
    post = fixture("post_tool_use_agent.json", repo)
    post["tool_response"]["content"][0]["text"] = f"Implemented.\npiece: media-upload\nDONE {sha[:9]}"
    stop = fixture("subagent_stop_background.json", repo)
    stop["session_id"] = post["session_id"]
    stop["last_assistant_message"] = f"Implemented.\npiece: media-upload\nDONE {sha[:9]}"
    given = []
    for data in ([stop, post] if stop_first else [post, stop]):
        if out := run("agent-result", data, capsys, monkeypatch):
            given.append(context(out, "PostToolUse"))
    notification = fixture("user_prompt_submit_task_notification.json", repo)
    notification["session_id"] = post["session_id"]
    if out := run("notes", notification, capsys, monkeypatch):
        given.append(context(out, "UserPromptSubmit"))
    line = f"benchbook: re-running the checks of media-upload at {sha[:7]} in the background"
    assert "\n".join(given).count(line) == 1, given
    requests = list(hooks._spool_dir().glob("*.json"))
    assert len(requests) == (1 if stop_first else 2)  # PostToolUse first cannot tell SubagentStop's report is its own
    assert hooks.replay_now(hooks._spool_dir()).recorded == len(requests)
    if stop_first:  # the lead merges before reading the verdict: the merge gate hands it over
        from benchbook import merge_gate

        gate = merge_gate.decide({"session_id": post["session_id"], "cwd": str(repo), "tool_name": "Bash",
                                  "tool_input": {"command": "git merge --no-ff media-upload"}}, wait=1)
        assert gate is not None
        assert gate["hookSpecificOutput"]["additionalContext"].startswith(f"benchbook: media-upload {sha[:7]}: re-run "
                                                                          "passed")
        assert not run("notes", notification, capsys, monkeypatch)
        return
    verdict = context(run("notes", notification, capsys, monkeypatch), "UserPromptSubmit")
    assert verdict.count(f"benchbook: media-upload {sha[:7]}: re-run passed") == 1 and line not in verdict
    assert not run("notes", notification, capsys, monkeypatch)


@pytest.mark.parametrize("failed_note", [False, True], ids=["spool-write-failed", "note-write-failed"])
def test_enqueue_failure_leaves_an_honest_notice_and_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              failed_note: bool) -> None:
    from benchbook import hooks, session_notes
    from benchbook.verify_requests import enqueue

    monkeypatch.setenv("BENCHBOOK_PROJECT", "review")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = make_repo(tmp_path / "repo")
    sha = piece(repo, "media-upload", {"impl.txt": "fixed\n"})
    post = fixture("post_tool_use_agent.json", repo)
    report = f"Implemented.\npiece: media-upload\nDONE {sha[:9]}"
    post["tool_response"]["content"][0]["text"] = report
    stop = fixture("subagent_stop_background.json", repo)
    stop["session_id"] = post["session_id"]
    stop["last_assistant_message"] = report
    spool = hooks._spool_dir()
    launches: list[tuple[Path, str]] = []
    monkeypatch.setattr(hooks, "start_processor", lambda path, lane: launches.append((path, lane)))

    def fail(*args: Any) -> None:
        raise OSError("injected write failure")

    target, method = (session_notes, "write_note") if failed_note else (hooks, "_write_spool")
    with monkeypatch.context() as patch:
        patch.setattr(target, method, fail)
        with pytest.raises(OSError, match="injected write failure"):
            enqueue(stop)

    assert session_notes.deliver(post["session_id"]) == ""
    assert len(list(spool.glob("*.json"))) == int(failed_note)
    assert launches == ([(spool, hooks.VERIFY)] if failed_note else [])
    assert f"re-running the checks of media-upload at {sha[:7]}" in enqueue(post)
    assert len(list(spool.glob("*.json"))) == int(failed_note) + 1
    assert launches[-1] == (spool, hooks.VERIFY)


@pytest.mark.parametrize("change", ["text", "reporter"])
def test_pending_notice_does_not_suppress_another_report_for_the_same_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    from benchbook import hooks, session_notes
    from benchbook.verify_requests import enqueue

    monkeypatch.setenv("BENCHBOOK_PROJECT", "review")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(hooks, "start_processor", lambda *args: None)
    repo = make_repo(tmp_path / "repo")
    sha = piece(repo, "media-upload", {"impl.txt": "fixed\n"})
    first = f"Implemented.\npiece: media-upload\nDONE {sha[:9]}"
    same = fixture("post_tool_use_agent.json", repo)
    same["tool_response"]["content"][0]["text"] = f"  {first}\n"
    stop = fixture("subagent_stop_background.json", repo)
    stop["session_id"] = same["session_id"]
    stop["last_assistant_message"] = first
    different = fixture("post_tool_use_agent.json", repo)
    different["tool_response"]["content"][0]["text"] = first
    if change == "text":
        different["tool_response"]["content"][0]["text"] = f"Checked again.\n{first}"
    else:
        different["tool_input"]["subagent_type"] = "another-worker"

    assert not enqueue(stop)
    assert f"re-running the checks of media-upload at {sha[:7]}" in enqueue(different)
    assert not enqueue(same)  # its SubagentStop already queued it even though the notice is delivered
    assert not session_notes.deliver(same["session_id"])
    assert len(list(hooks._spool_dir().glob("*.json"))) == 2


# ------------------------------------------------------------------------------------------- who decides a re-run


def _report_and_replay(repo: Path, sha: str, capsys: pytest.CaptureFixture[str],
                       monkeypatch: pytest.MonkeyPatch) -> str:
    """An implementer's foreground report of sha, its re-run in the spool processor, and the line it leaves."""
    from benchbook import hooks, session_notes

    data = fixture("post_tool_use_agent.json", repo)
    data["tool_response"]["content"][0]["text"] = f"Implemented.\npiece: media-upload\nDONE {sha[:9]}"
    monkeypatch.setattr(hooks, "start_processor", lambda *args: None)
    run("agent-result", data, capsys, monkeypatch)
    assert hooks.replay_now(hooks._spool_dir()).recorded == 1
    return session_notes.deliver(data["session_id"])


def _claim_status(db_url: str, project: str, sha: str) -> str:
    lg = agent(db_url, project, "lead")
    [claim] = lg.sql_internal("SELECT id FROM benchbook.claims WHERE project_id = %s AND kind = 'completion'"
                              " AND %s = ANY(tags)", (project, f"commit:{sha}"))
    return str(lg.claim_status(claim["id"]))


@pytest.mark.parametrize("first", ["check-verify", "agent-result"])
def test_the_re_run_decides_whichever_command_ran_first(first: str, code: str, env: Path, db_url: str,
                                                        capsys: pytest.CaptureFixture[str],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #4 issue 5: the hook's re-run is rerun@<host> (orchestrator), check verify without --agent is
    verifier@<host> (harness). With one shared name, whichever ran first in a project fixed the role for both."""
    from benchbook import checks

    repo = make_repo(env / "repo", CONFIG.replace('project = "ignored"', f'project = "{code}"'))
    by_hand = piece(repo, "by-hand", {"impl.txt": "fixed\n"})
    reported = piece(repo, "media-upload", {"impl.txt": "fixed\n", "value.txt": "ok\n"})

    def verify_by_hand() -> str:
        p = subprocess.run([sys.executable, "-m", "benchbook.cli", "--project", code, "check", "verify", "by-hand",
                            by_hand, "--reported-by", "impl"], cwd=repo, env=dict(os.environ), capture_output=True,
                           text=True, timeout=180)
        assert p.returncode == 0, p.stderr
        return p.stdout

    if first == "check-verify":
        by_hand_out, note = verify_by_hand(), _report_and_replay(repo, reported, capsys, monkeypatch)
    else:
        note, by_hand_out = _report_and_replay(repo, reported, capsys, monkeypatch), verify_by_hand()
    assert "decision: recorded without a decision" in by_hand_out
    assert "re-run passed (2 checks" in note and "checked: re-run (c:" in note
    assert _claim_status(db_url, code, reported) == "verified"
    assert _claim_status(db_url, code, by_hand) == "proposed"
    lg = agent(db_url, code, "lead")
    roles = {r["id"]: r["role"] for r in lg.sql_internal("SELECT id, role FROM benchbook.agents WHERE project_id = %s",
                                                         (code,))}
    assert roles[checks.default_verifier()] == "harness" and roles[checks.default_rerunner()] == "orchestrator"


@pytest.mark.parametrize(("role", "checked"), [
    ("orchestrator", "checked: re-run (c:"),
    ("worker", "checked: none (recorded by a worker agent; a lead decides) (c:"),
])
def test_under_a_token_the_re_run_is_the_tokens_agent(role: str, checked: str, code: str, env: Path, db_url: str,
                                                      capsys: pytest.CaptureFixture[str],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """A token names the agent and its role, as before issue 5; a line that decided nothing says why."""
    from benchbook import checks
    from tests.writer import as_writer, issue

    repo = make_repo(env / "repo", CONFIG.replace('project = "ignored"', f'project = "{code}"'))
    sha = piece(repo, "media-upload", {"impl.txt": "fixed\n"})
    monkeypatch.setenv("BENCHBOOK_TOKEN", issue(db_url, code, "tok-agent", role))
    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    note = _report_and_replay(repo, sha, capsys, monkeypatch)
    assert checked in note
    monkeypatch.delenv("BENCHBOOK_TOKEN")  # read back as the owner
    assert _claim_status(db_url, code, sha) == ("verified" if role == "orchestrator" else "proposed")
    lg = agent(db_url, code, "lead")
    [author] = lg.sql_internal("SELECT DISTINCT author FROM benchbook.experiments"
                               " WHERE project_id = %s AND code_ref = %s", (code, sha))
    assert author["author"] == "tok-agent"
    assert not lg.sql_internal("SELECT 1 FROM benchbook.agents WHERE project_id = %s AND id = %s",
                               (code, checks.default_rerunner()))


# ------------------------------------------------------------------------------------------- the lead's own records

def test_a_code_projects_session_start_guides_the_lead_even_with_an_empty_ledger(
        db_url: str, code: str, env: Path, capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm F's lead in the boundary pilot had no skill: it never asked its workers for findings and recorded no dead
    end. A new (not resumed) session in a code project now starts with how to."""
    cfg = (CONFIG.replace('project = "ignored"', f'project = "{code}"')
           + '\n[report]\nout_of_scope_heading = "Out of scope"\n')
    repo = make_repo(env / "repo", cfg)
    text = context(run("session-start", fixture("session_start_startup.json", repo), capsys, monkeypatch))
    assert text.startswith("benchbook records this repository's checks and reports.")
    assert "under a `## Out of scope` heading" in text and "benchbook --agent lead discard <piece>" in text
    assert "# benchbook brief" not in text  # the ledger is empty: the guide alone
    resumed = {**fixture("session_start_startup.json", repo), "source": "resume"}
    assert not run("session-start", resumed, capsys, monkeypatch)
    w = boundary_hooks.where(fixture("session_start_startup.json", repo))
    assert w is not None and "waits for it" in text  # the merge gate is the Claude Code plugin's alone
    assert "waits for it" not in boundary_hooks.lead_guide({"source": "startup"}, w, "codex")


def test_the_leads_dead_ends_and_findings_reach_the_brief(db_url: str, code: str, env: Path,
                                                          capsys: pytest.CaptureFixture[str],
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_code_brief import SCRIPT

    repo = make_repo(env / "repo", CONFIG.replace('project = "ignored"', f'project = "{code}"'))
    cli_env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url, "XDG_STATE_HOME": str(env / "state")}

    def cli(*args: str) -> str:
        p = subprocess.run([SCRIPT, "--agent", "lead", *args], capture_output=True, text=True, cwd=repo,
                           timeout=120, env=cli_env)
        assert p.returncode == 0, p.stderr
        return p.stdout

    assert "recorded" in cli("discard", "camel-to-kebab", "--approach", "regex-split",
                             "--why", "re.split drops the acronym boundary in HTTPServer")
    assert "already recorded" in cli("discard", "camel-to-kebab", "--approach", "regex-split",
                                     "--why", "re.split drops the acronym boundary in  HTTPServer")
    assert "recorded" in cli("finding", "add", "strutils.slugify keeps a trailing dash", "--file",
                             "boltons/strutils.py")
    text = context(run("session-start", fixture("session_start_startup.json", repo), capsys, monkeypatch))
    discarded = text.split("## Discarded approaches")[1].split("\n## ")[0]
    assert "regex-split on camel-to-kebab: noted: re.split drops the acronym boundary in HTTPServer" in discarded
    assert "×1" in discarded
    findings = text.split("## Open out-of-scope findings")[1].split("\n## ")[0]
    assert "strutils.slugify keeps a trailing dash" in findings and "boltons/strutils.py" in findings
