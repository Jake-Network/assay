"""Cross-harness hook manifests, shell results and boundary context.

Fixtures (tests/fixtures/hooks/<harness>/, the sources are in docs/harnesses.md, "Fixtures"): the command and the
output of the Claude Code fixtures (post_tool_use_bash.json, post_tool_use_failure_bash.json) in each harness's
documented input, so the same run arrives through every harness. Acceptance (spec §8, P1): each harness's stored
fixture yields the attempt the Claude Code fixture of the same run yields.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from benchbook import Ledger, doctor, harnesses
from benchbook.checks import last_line_hash
from benchbook.entry import hook_args
from benchbook.harnesses import shell_call
from tests.conftest import make_project
from tests.test_check_hooks import FX, GATE_CONFIG, ROOT, fixture, hook_log, observed
from tests.test_check_run import PY, SCRIPT, attempts, cli_env, make_checkout, wait_attempts
from tests.test_code_brief import _finding

# the last error line of the failing fixture: the result count after it ("1 failed, 1 passed in 0.42s") is not one
PASS_LINE, FAIL_LINE = "2 passed in 0.31s", "FAILED tests/test_impl.py::test_fixed - AssertionError: took 0.42s"
# harness: (passing run, failing run) of the same commands as the Claude Code fixtures
RUNS = {"codex": ("codex/post_tool_use_bash.json", "codex/post_tool_use_bash_failure.json"),
        "cursor": ("cursor/post_tool_use_shell.json", "cursor/post_tool_use_shell_failure.json"),
        "gemini": ("gemini/after_tool_shell.json", "gemini/after_tool_shell_failure.json"),
        "opencode": ("opencode/execute_after_shell.json", "opencode/execute_after_shell_failure.json"),
        "pi": ("pi/tool_result_bash.json", "pi/tool_result_bash_failure.json")}


@pytest.fixture
def code_project(db_url: str) -> str:
    return make_project(db_url, metric="checks_passed", direction="max", pack="code",
                        settings={"partition_key": "piece"})


@pytest.fixture
def lg(db_url: str, code_project: str) -> Ledger:
    return Ledger.open(project=code_project, agent="lead", database_url=db_url)


@pytest.fixture
def gate_repo(tmp_path: Path, code_project: str) -> Path:
    return make_checkout(tmp_path / "repo", code_project, cfg=GATE_CONFIG.format(project=code_project, py=PY))


def load(name: str, cwd: Path | str = "/project", command: str = "uv run pytest tests -q") -> dict[str, Any]:
    raw = (FX / name).read_text()
    for k, v in (("CWD", str(cwd)), ("COMMAND", command)):
        raw = raw.replace("{" + k + "}", json.dumps(v)[1:-1])
    data: dict[str, Any] = json.loads(raw)
    return data


def run_hook(event: str, harness: str | None, data: dict[str, Any], env: dict[str, str],
             cwd: Path) -> subprocess.CompletedProcess[str]:
    argv = [SCRIPT, "hook", event] + (["--harness", harness] if harness else [])
    return subprocess.run(argv, input=json.dumps(data), text=True, capture_output=True, cwd=cwd, env=env, timeout=120)


# ------------------------------------------------------------------------------------------- the parsers

@pytest.mark.parametrize("harness", sorted(RUNS))
def test_each_harness_reads_the_command_and_the_outcome_of_its_fixture(harness: str, tmp_path: Path) -> None:
    ok_name, bad_name = RUNS[harness]
    call = shell_call(harness, load(ok_name, tmp_path, "true"))
    assert call is not None and (call.harness, call.command, call.cwd) == (harness, "true", tmp_path)
    ok = call.outcome()
    assert (ok.status, ok.exit_code, ok.line_hash, ok.shape_ok) == ("ok", 0, None, True)
    bad_call = shell_call(harness, load(bad_name, tmp_path))
    assert bad_call is not None
    bad = bad_call.outcome()
    # the exit code and the rough signature's input, the last output line, are the Claude Code fixture's
    assert (bad.status, bad.exit_code, bad.shape_ok) == ("fail", 1, True)
    assert bad.line_hash == last_line_hash(FAIL_LINE)
    cc = harnesses.claude_code_outcome(fixture("post_tool_use_failure_bash.json", tmp_path))
    assert (cc.status, cc.exit_code, cc.line_hash) == (bad.status, bad.exit_code, bad.line_hash)


def test_a_harness_reads_only_its_own_shell_tool() -> None:
    readers = {harness: {harness} for harness in RUNS}
    readers["codex"].add("claude-code")  # both report Bash
    for harness, (name, _) in RUNS.items():
        data = load(name)
        assert {h for h in harnesses.HARNESSES if shell_call(h, data) is not None} == readers[harness]
        assert shell_call(harness, {**data, "tool_name": "Read", "tool": "read", "toolName": "read"}) is None
    with pytest.raises(ValueError, match="unknown harness"):
        shell_call("vscode", {})


def test_codex_without_an_exit_code_is_not_a_pass() -> None:
    """The exec_command path sends the output alone (codex_context.rs): the outcome is unknown, not ok."""
    raw = harnesses.codex_outcome(load("codex/post_tool_use_exec_command.json"))
    assert (raw.status, raw.exit_code, raw.shape_ok) == (None, None, True) and "without its exit code" in raw.reason
    for resp in ("", PASS_LINE, "Output:\n" + PASS_LINE, "Process exited with code 0\nOutput:\nok",
                 "Wall time: 1.0 seconds\nProcess running with session ID 7\nOutput:\npartial"):
        assert harnesses.codex_outcome({"tool_response": resp}).status is None, resp
    odd = harnesses.codex_outcome({"tool_response": {"stdout": PASS_LINE}})
    assert (odd.status, odd.shape_ok) == (None, False)
    chunked = harnesses.codex_outcome({"tool_response": "Chunk ID: c1\nWall time: 0.5000 seconds\n"
                                                        "Process exited with code 2\nOriginal token count: 9\n"
                                                        "Output:\nboom\n"})
    assert (chunked.status, chunked.exit_code, chunked.line_hash) == ("fail", 2, last_line_hash("boom"))


def test_cursor_failures_carry_no_exit_code() -> None:
    fail = load("cursor/post_tool_use_failure_shell.json")
    res = harnesses.cursor_outcome(fail)
    assert (res.status, res.exit_code, res.shape_ok) == (None, None, True)
    assert harnesses.cursor_outcome({**fail, "failure_type": "timeout"}).status == "timeout"
    assert harnesses.cursor_outcome({**fail, "failure_type": "timeout", "is_interrupt": True}).status is None
    assert harnesses.cursor_outcome({**fail, "failure_type": "permission_denied"}).status is None
    assert harnesses.cursor_outcome({**fail, "failure_type": "crash"}).shape_ok is False
    ok = load("cursor/post_tool_use_shell.json")
    for out in ("All tests passed", json.dumps({"stdout": "x"}), json.dumps({"exitCode": "0"}),
                json.dumps({"exitCode": True}), None):
        res = harnesses.cursor_outcome({**ok, "tool_output": out})
        assert (res.status, res.shape_ok) == (None, False), out
    assert harnesses.cursor_outcome({**ok, "tool_output": '{"exitCode": 3}'}).line_hash is None  # no stdout


def test_gemini_reads_the_exit_code_line_and_nothing_else() -> None:
    def out(text: str, **ti: Any) -> harnesses.Outcome:
        return harnesses.gemini_outcome({"tool_input": {"command": "x", **ti}, "tool_response": {"llmContent": text}})

    assert (out("Output: (empty)\nProcess Group PGID: 1").status, out("Output: ok").exit_code) == ("ok", 0)
    bad = out("Output: a\nlast line\nError: spawn\nExit Code: 127\nSignal: 9\nProcess Group PGID: 1")
    assert (bad.status, bad.exit_code, bad.line_hash) == ("fail", 127, last_line_hash("last line"))
    assert out("Output: x\nSignal: SIGKILL\nProcess Group PGID: 1").status is None
    assert out("Output: (empty)\nError: spawn sh ENOENT").status is None
    assert out("Command was automatically cancelled because it exceeded the timeout of 5.0 minutes without "
               "output. There was no output before it was cancelled.").status == "timeout"
    assert out("Command was cancelled by user before it could complete.").status is None
    assert out("Command moved to background (PID: 9). Output hidden. Press Ctrl+B to view.").status is None
    assert out("Output: ok", is_background=True).status is None
    assert out("<untrusted>Output: ok</untrusted>").shape_ok is False  # wrapped or summarised: not read
    assert harnesses.gemini_outcome({"tool_response": "Output: ok"}).shape_ok is False


def test_the_fixture_version_doctor_names_is_the_fixtures() -> None:
    versions = {json.loads(p.read_text())["cursor_version"] for p in (FX / "cursor").glob("*.json")}
    assert versions == {harnesses.FIXTURE_VERSIONS["cursor"]}


def test_native_shell_events_require_an_integer_exit_code() -> None:
    open_ = load(RUNS["opencode"][0])
    pi = load(RUNS["pi"][0])
    missing = (
        harnesses.opencode_outcome({**open_, "result": {"output": {"exitCode": None}}}),
        harnesses.opencode_outcome({**open_, "status": "error"}),
        harnesses.opencode_outcome({**open_, "result": {"output": {"exitCode": True}}}),
        harnesses.pi_outcome({**pi, "structuredContent": None, "isError": True}),
        harnesses.pi_outcome({**pi, "structuredContent": {"exit_code": True}}),
    )
    assert all(o.status is None and o.exit_code is None for o in missing)


# ------------------------------------------------------------------------------------------- the same attempt

def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """What the acceptance compares: piece, check, outcome, signature (in metrics), via, commit, author."""
    return {"piece": row["config"]["piece"], "checks": row["config"]["checks"], "code_ref": row["code_ref"],
            "run_status": row["run_status"], "author": row["author"], "metrics": row["metrics"]}


@pytest.mark.parametrize("harness", sorted(RUNS))
def test_the_same_attempt_comes_out_of_every_harness(harness: str, gate_repo: Path, tmp_path: Path,
                                                    lg: Ledger) -> None:
    env = cli_env(tmp_path)
    unit, quick = f"{PY} check_ok.py", "true"
    for name, cmd in (("post_tool_use_bash.json", unit), ("post_tool_use_failure_bash.json", quick)):
        assert run_hook("bash-result", None, fixture(name, gate_repo, cmd), env, gate_repo).returncode == 0
    claude = wait_attempts(lg, 2)
    assert len(claude) == 2
    ok_name, bad_name = RUNS[harness]
    answer = harnesses.answer(harness)
    for name, cmd in ((ok_name, unit), (bad_name, quick), (ok_name, "ls -la")):
        p = run_hook("bash-result", harness, load(name, gate_repo, cmd), env, gate_repo)
        assert (p.returncode, p.stdout, p.stderr) == (0, answer, ""), p.stderr
    rows = wait_attempts(lg, 4)
    theirs = [r for r in rows if r["id"] not in {c["id"] for c in claude}]
    assert len(theirs) == 2

    def by_check(rs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {r["config"]["checks"][0]: _shape(r) for r in rs}

    assert by_check(theirs) == by_check(claude)
    failed = by_check(theirs)["quick"]["metrics"]
    assert failed["failure_signature"] == f"quick:exit 1:{last_line_hash(FAIL_LINE)}" and failed["via"] == "bash-hook"
    assert "SECRET-OUTPUT" not in json.dumps(rows, default=str)
    assert all(f"harness {harness}" in r["notes"] for r in theirs)
    if harness == "cursor":
        assert sorted(r["duration_ms"] for r in theirs) == [1234, 4187]
    # `ls -la` is not a declared check: no observation; the four runs that are each left one, with their harness
    assert [(o["harness"], o["kind"]) for o in observed(tmp_path)] == [("claude-code", "direct")] * 2 + [
        (harness, "direct")] * 2


def test_codex_output_without_an_exit_code_is_observed_and_not_recorded(gate_repo: Path, tmp_path: Path,
                                                                        lg: Ledger) -> None:
    env = cli_env(tmp_path)
    run_hook("bash-result", "codex", load("codex/post_tool_use_exec_command.json", gate_repo, "true"), env, gate_repo)
    run_hook("bash-result", "cursor", load("cursor/post_tool_use_failure_shell.json", gate_repo, "true"), env,
             gate_repo)
    lines = observed(tmp_path)
    assert [(o["harness"], o["kind"], o["exit_code"]) for o in lines] == [("codex", "unrecordable", None),
                                                                         ("cursor", "unrecordable", None)]
    assert "without its exit code" in lines[0]["reason"] and "failure_type error" in lines[1]["reason"]
    assert "input_shape" not in lines[0] and lines[1]["harness_version"] == "1.7.2"
    assert not list((tmp_path / "state").glob("benchbook/spool/*/*.json"))
    time.sleep(0.5)
    assert attempts(lg) == []


def test_an_unknown_harness_is_logged_and_the_call_goes_on(gate_repo: Path, tmp_path: Path) -> None:
    p = run_hook("bash-result", "vscode", load(RUNS["cursor"][0], gate_repo, "true"), cli_env(tmp_path), gate_repo)
    assert (p.returncode, p.stdout, p.stderr) == (0, "", "")
    assert "unknown harness 'vscode'" in hook_log(tmp_path)


# ------------------------------------------------------------------------------------------- reports

def _notes(lg: Ledger) -> list[dict[str, Any]]:
    return lg.sql_internal("SELECT * FROM benchbook.claims WHERE project_id=%s AND 'auto-captured' = ANY(tags)",
                           (lg.project_id,))


def _wait_notes(lg: Ledger, n: int, timeout: float = 60) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while len(rows := _notes(lg)) < n and time.monotonic() < deadline:
        time.sleep(0.2)
    return rows


@pytest.mark.parametrize("harness", ["codex", "cursor"])
def test_subagent_reports_are_captured(harness: str, gate_repo: Path, tmp_path: Path, lg: Ledger) -> None:
    """Codex's SubagentStop and Cursor's subagentStop reach the same processor as Claude Code's. Cursor's subagent
    types are its own (generalPurpose, ...), so BENCHBOOK_HOOK_AGENTS must name one."""
    data = load(f"{harness}/subagent_stop.json", gate_repo)
    answer = "" if harness == "codex" else "{}\n"
    if harness == "cursor":
        data.pop("cwd", None)  # subagentStop has none: workspace_roots names the project
        p = run_hook("subagent-stop", harness, data, cli_env(tmp_path), tmp_path)  # a hook cwd outside the project
        assert (p.returncode, p.stdout) == (0, answer)
        assert not list((tmp_path / "state").glob("benchbook/spool/*/*.json"))  # generalPurpose is no worker
        env = cli_env(tmp_path, BENCHBOOK_HOOK_AGENTS="generalPurpose")
    else:
        env = cli_env(tmp_path)
    p = run_hook("subagent-stop", harness, data, env, tmp_path)
    assert (p.returncode, p.stdout, p.stderr) == (0, answer, "")
    [note] = _wait_notes(lg, 1)
    assert note["payload"]["text"] == "Piece media done. The upload test failed before the fix and passes after it; " \
                                      "no records cited."
    source = note["payload"]["source"]
    assert source["harness"] == harness and source["event"] == "SubagentStop"
    assert source["agent_type"] == ("benchbook-worker" if harness == "codex" else "generalPurpose")
    assert (harness == "cursor") == (source.get("harness_version") == "1.7.2")


def test_report_input_drops_the_transcripts_it_cannot_read() -> None:
    for harness in ("codex", "cursor"):
        data = harnesses.report_input(harness, load(f"{harness}/subagent_stop.json"))
        assert "agent_transcript_path" not in data and "transcript_path" not in data
        assert data["last_assistant_message"].startswith("Piece media done.") and data["cwd"] == "/project"
    cc = {"agent_transcript_path": "/t.jsonl", "last_assistant_message": "x"}
    assert harnesses.report_input("claude-code", cc) is cc


# ------------------------------------------------------------------------------------------- doctor

def test_doctor_names_the_harness_version_whose_input_it_cannot_read(gate_repo: Path, tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    env = cli_env(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    assert doctor.check_harness_input() is None  # nothing observed
    run_hook("bash-result", None, fixture("post_tool_use_bash.json", gate_repo, "true"), env, gate_repo)
    assert doctor.check_harness_input() is None  # Claude Code only
    run_hook("bash-result", "cursor", load(RUNS["cursor"][0], gate_repo, "true"), env, gate_repo)
    good = doctor.check_harness_input()
    assert good is not None and good.ok and "cursor 1.7.2: 1 check run(s) read" in good.detail
    changed = {**load(RUNS["cursor"][0], gate_repo, "true"), "cursor_version": "3.0.0",
               "tool_output": json.dumps({"exit_status": 0, "output": PASS_LINE})}
    run_hook("bash-result", "cursor", changed, env, gate_repo)
    run_hook("bash-result", "codex", {**load(RUNS["codex"][0], gate_repo, "true"), "tool_response": {"x": 1}}, env,
             gate_repo)
    bad = doctor.check_harness_input()
    assert bad is not None and not bad.ok and bad.fix and "benchbook check run" in bad.fix
    assert "cursor 3.0.0: 1 of 1 check run(s)" in bad.detail and "stored fixtures are of cursor 1.7.2" in bad.detail
    assert "codex (no version in its input): 1 of 1" in bad.detail and "cursor 1.7.2:" not in bad.detail
    [line] = [o for o in observed(tmp_path) if o.get("harness_version") == "3.0.0"]
    assert (line["kind"], line["input_shape"]) == ("unrecordable", "unexpected")


# ------------------------------------------------------------------------------------------- wiring

def _context(out: str, harness: str, event: str) -> str:
    if harness in ("opencode", "pi"):
        return out
    parsed = json.loads(out)
    if harness == "cursor":
        return parsed["additional_context"]
    data = parsed["hookSpecificOutput"]
    if harness in ("claude-code", "codex"):
        assert data["hookEventName"] == event
    else:
        assert set(data) == {"additionalContext"}
    return data["additionalContext"]


@pytest.mark.parametrize("harness", sorted(RUNS))
def test_same_boundary_brief_and_one_shot_notes(harness: str, gate_repo: Path, tmp_path: Path, lg: Ledger,
                                              db_url: str) -> None:
    env = cli_env(tmp_path, BENCHBOOK_DATABASE_URL=db_url)
    finding = "The media cache misses invalidation on rename."
    _finding(lg, finding, ["piece:media"])
    original = run_hook("session-start", None, fixture("session_start_startup.json", gate_repo), env, tmp_path)
    assert original.returncode == 0 and original.stderr == ""
    expected = _context(original.stdout, "claude-code", "SessionStart")
    assert finding in expected
    start = load(f"{harness}/session_start.json", gate_repo)
    got = run_hook("session-start", harness, start, env, tmp_path)
    assert got.returncode == 0 and got.stderr == ""
    head, body = _context(got.stdout, harness, "SessionStart").split("\n", 1)
    expected_head, expected_body = expected.split("\n", 1)
    assert head.rsplit(" · ", 1)[0] == expected_head.rsplit(" · ", 1)[0]
    assert body == expected_body

    tool = start if harness == "opencode" else load(RUNS[harness][0], gate_repo)
    session_id = harnesses.boundary_input(harness, start)["session_id"]
    assert harnesses.boundary_input(harness, tool)["session_id"] == session_id
    notes = tmp_path / "state" / "benchbook" / "notes" / session_id
    notes.mkdir(parents=True)
    (notes / "note.md").write_text("benchbook: rerun passed.")
    delivered = run_hook("notes", harness, tool, env, tmp_path)
    assert delivered.returncode == 0 and delivered.stderr == ""
    assert _context(delivered.stdout, harness, "PostToolUse").rstrip("\n") == "benchbook: rerun passed."
    empty = run_hook("notes", harness, tool, env, tmp_path)
    assert (empty.returncode, empty.stderr, empty.stdout) == (0, "", harnesses.answer(harness))
    assert (notes / "delivered" / "note.md").is_file()

def _commands(entries: list[dict[str, Any]]) -> list[str]:
    return [h["command"] for e in entries for h in e.get("hooks", [e])]


def test_the_manifests_run_the_hooks_with_their_harness() -> None:
    codex = json.loads((ROOT / "integrations" / "codex" / "hooks.json").read_text())["hooks"]
    assert set(codex) == {"SessionStart", "PostToolUse", "SubagentStop"}
    post, notes = codex["PostToolUse"]
    assert post["matcher"] == "^Bash$" and _commands([post]) == ["benchbook hook bash-result --harness codex"]
    assert "matcher" not in notes and _commands([notes]) == ["benchbook hook notes --harness codex"]
    assert _commands(codex["SessionStart"]) == ["benchbook hook session-start --harness codex"]
    assert "matcher" not in codex["SessionStart"][0]
    assert "matcher" not in codex["SubagentStop"][0]
    assert _commands(codex["SubagentStop"]) == ["benchbook hook subagent-stop --harness codex"]
    assert all(h["type"] == "command" and h["timeout"] >= 5 for e in codex.values() for g in e for h in g["hooks"])
    cursor = json.loads((ROOT / "integrations" / "cursor" / "hooks.json").read_text())
    assert cursor["version"] == 1
    h = cursor["hooks"]
    assert set(h) == {"sessionStart", "postToolUse", "postToolUseFailure", "subagentStop"}
    for event in ("postToolUse", "postToolUseFailure"):
        e, notes = h[event]
        assert e == {"command": "benchbook hook bash-result --harness cursor", "matcher": "Shell", "timeout": 5}
        assert notes == {"command": "benchbook hook notes --harness cursor", "timeout": 5}
    assert _commands(h["sessionStart"]) == ["benchbook hook session-start --harness cursor"]
    assert h["subagentStop"] == [{"command": "benchbook hook subagent-stop --harness cursor", "timeout": 5}]
    gemini = json.loads((ROOT / "integrations" / "gemini" / "hooks.json").read_text())["hooks"]
    assert set(gemini) == {"SessionStart", "AfterTool"}
    assert _commands(gemini["SessionStart"]) == ["benchbook hook session-start --harness gemini"]
    assert _commands(gemini["AfterTool"]) == ["benchbook hook bash-result --harness gemini",
                                            "benchbook hook notes --harness gemini"]
    assert gemini["AfterTool"][0]["matcher"] == "^run_shell_command$"
    assert all(x["timeout"] >= 5000 for e in gemini.values() for g in e for x in g["hooks"])
    for manifest in (codex, h, gemini):
        for entries in manifest.values():
            for cmd in _commands(entries):
                argv = cmd.split()
                assert argv[0] == "benchbook" and (parsed := hook_args(argv[1:])) is not None
                assert parsed[1] in harnesses.HARNESSES
    for name in ("codex", "cursor"):
        readme = (ROOT / "integrations" / name / "README.md").read_text()
        assert "completion gate" in readme and "not wired" in readme and "docs/harnesses.md" in readme


def test_hook_arguments() -> None:
    assert hook_args(["hook", "bash-result"]) == ("bash-result", None)
    assert hook_args(["hook", "bash-result", "--harness", "codex"]) == ("bash-result", "codex")
    assert hook_args(["hook", "subagent-stop", "--harness=cursor"]) == ("subagent-stop", "cursor")
    assert hook_args(["hook", "replay"]) is None and hook_args(["hook", "bash-result", "--json"]) is None
    assert hook_args(["hook", "bash-result", "--harness"]) is None


def test_the_cli_spelling_takes_the_harness_too(gate_repo: Path, tmp_path: Path) -> None:
    import sys

    p = subprocess.run([sys.executable, "-m", "benchbook.cli", "hook", "bash-result", "--harness", "cursor"],
                       cwd=gate_repo, input=json.dumps(load(RUNS["cursor"][1], gate_repo, "true")), text=True,
                       capture_output=True, env=cli_env(tmp_path), timeout=60)
    assert (p.returncode, p.stdout) == (0, "{}\n")
    assert [(o["harness"], o["kind"], o["exit_code"]) for o in observed(tmp_path)] == [("cursor", "direct", 1)]
