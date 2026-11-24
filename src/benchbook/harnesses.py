"""What each harness's hook input says about a shell call and a subagent's report (spec §7, P1).

`benchbook hook bash-result` and `benchbook hook subagent-stop` run under Claude Code by default. The manifests in
integrations/codex/ and integrations/cursor/ add `--harness codex` or `--harness cursor` to the command, and this
module turns shell results, reports and boundary events into what the hook handlers read. Gemini CLI uses the
same command-hook interface. OpenCode and Pi use small native extensions to forward their SDK events.

Each reading is quoted from the harness documentation in docs/harnesses.md; a field the documentation does not
name is not read. When the outcome cannot be known (Codex sends no exit code on most shell paths, a Cursor
failure has none) the run is observed as unrecordable instead of being recorded as a pass. `shape_ok` is False when
the input is not shaped the way the documentation (and the stored fixtures, tests/fixtures/hooks/<harness>/) say:
`benchbook doctor` reports those, with the harness version where the input carries one (Cursor).

Imported only by the hook path, and only after the input is read: stdlib only, no database module.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .check_hooks import Outcome, rough_signature
from .check_hooks import outcome as claude_code_outcome

CLAUDE_CODE = "claude-code"
HARNESSES = (CLAUDE_CODE, "codex", "cursor", "gemini", "opencode", "pi")
# The harness version in the stored fixtures, for doctor's message (only Cursor's input carries a version).
FIXTURE_VERSIONS = {"cursor": "1.7.2"}
# Harnesses that read the hook's stdout as JSON: an empty answer is "{}". Cursor counts "no output" as a hook failure
# (cursor_hooks.md, failClosed); Gemini parses stdout as JSON on exit 0 (gemini_hooks_ref.md, Global hook mechanics).
JSON_ANSWER = frozenset({"cursor", "gemini"})


@dataclass
class ShellCall:
    """A shell command one harness ran, in the terms check_hooks records."""
    harness: str
    command: str
    cwd: Path
    data: dict[str, Any]
    session_id: str | None = None
    tool_use_id: str | None = None
    agent_type: str | None = None
    duration_ms: int = 0
    version: str | None = None

    def outcome(self) -> Outcome:
        return OUTCOMES[self.harness](self.data)


def check_harness(harness: str) -> str:
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r} (one of {', '.join(HARNESSES)})")
    return harness


def answer(harness: str) -> str:
    """What the hook prints on stdout when it has nothing to say."""
    return "{}\n" if harness in JSON_ANSWER else ""


# Command-hook output is event-specific. OpenCode and Pi read plain text in our extension and hand it to their SDK.
CONTEXT_EVENTS = {CLAUDE_CODE: {"session-start": "SessionStart", "notes": "UserPromptSubmit",
                                "agent-result": "PostToolUse"},
                  "codex": {"session-start": "SessionStart", "notes": "PostToolUse"},
                  # Gemini CLI reads only additionalContext; its output types still declare hookEventName
                  "gemini": {"session-start": "SessionStart", "notes": "AfterTool"}}
# Claude Code events the notes hook runs on: its answer names the one that ran (hookEventName must match it).
CLAUDE_CODE_NOTES_EVENTS = frozenset({"UserPromptSubmit", "PostToolUse", "PostToolUseFailure"})
PLAIN_EVENTS = {CLAUDE_CODE: frozenset({"pre-compact"}),
                "opencode": frozenset({"session-start", "notes"}),
                "pi": frozenset({"session-start", "notes"})}


def context_answer(harness: str, event: str, text: str, hook_event: object = None) -> str:
    """The harness-specific answer for a boundary event. hook_event: the input's hook_event_name, which for Claude
    Code's notes hook says which of its events ran."""
    if not text:
        return answer(harness)
    if event in PLAIN_EVENTS.get(harness, ()):
        return text if text.endswith("\n") else text + "\n"
    if (name := CONTEXT_EVENTS.get(harness, {}).get(event)) is not None:
        if harness == CLAUDE_CODE and event == "notes" and hook_event in CLAUDE_CODE_NOTES_EVENTS:
            name = str(hook_event)
        return json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}},
                          ensure_ascii=False) + "\n"
    if event in ("session-start", "notes") and harness == "cursor":
        return json.dumps({"additional_context": text}, ensure_ascii=False) + "\n"
    return answer(harness)


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _command(data: dict[str, Any], tool: str) -> tuple[str, dict[str, Any]] | None:
    ti = data.get("tool_input")
    if data.get("tool_name") != tool or not isinstance(ti, dict) or not isinstance(ti.get("command"), str):
        return None
    return ti["command"], ti


def _session_cwd(data: dict[str, Any]) -> Path:
    return Path(_str(data.get("cwd")) or os.getcwd())


def _cursor_root(data: dict[str, Any]) -> str | None:
    roots = data.get("workspace_roots")
    return _str(roots[0]) if isinstance(roots, list) and roots else None


def boundary_input(harness: str, data: dict[str, Any]) -> dict[str, Any]:
    """Give boundary hooks the session identifier and checkout in their common form."""
    check_harness(harness)
    if harness == "cursor":
        return {**data, "cwd": _str(data.get("cwd")) or _cursor_root(data),
                "session_id": _str(data.get("session_id")) or _str(data.get("conversation_id"))}
    if harness == "opencode":
        return {**data, "session_id": _str(data.get("sessionID"))}
    return data


# ---------------------------------------------------------------------------------------------- shell calls


def _claude_code_call(data: dict[str, Any]) -> ShellCall | None:
    if (c := _command(data, "Bash")) is None:
        return None
    return ShellCall(CLAUDE_CODE, c[0], _session_cwd(data), data, _str(data.get("session_id")),
                     _str(data.get("tool_use_id")), _str(data.get("agent_type")), _int(data.get("duration_ms")))


def _codex_call(data: dict[str, Any]) -> ShellCall | None:
    """PostToolUse with tool_name "Bash" (shell commands and exec_command both match as Bash). No duration and no
    agent type are documented for PostToolUse."""
    if (c := _command(data, "Bash")) is None:
        return None
    return ShellCall("codex", c[0], _session_cwd(data), data, _str(data.get("session_id")),
                     _str(data.get("tool_use_id")))


def _cursor_call(data: dict[str, Any]) -> ShellCall | None:
    """postToolUse / postToolUseFailure with tool_name "Shell". The command ran in tool_input.working_directory when
    given (preToolUse's example has it), else in cwd; a user-level hook itself runs from ~/.cursor/, so os.getcwd()
    is only the last resort."""
    if (c := _command(data, "Shell")) is None:
        return None
    command, ti = c
    cwd = Path(_str(ti.get("working_directory")) or _str(data.get("cwd")) or _cursor_root(data) or os.getcwd())
    return ShellCall("cursor", command, cwd, data, _str(data.get("conversation_id")), _str(data.get("tool_use_id")),
                     duration_ms=_int(data.get("duration")), version=_str(data.get("cursor_version")))


def _gemini_call(data: dict[str, Any]) -> ShellCall | None:
    """AfterTool with tool_name "run_shell_command". dir_path is resolved against the session's directory, as the
    shell tool does (gemini_shell.ts). No tool call id and no duration are documented."""
    if (c := _command(data, "run_shell_command")) is None:
        return None
    command, ti = c
    cwd = _session_cwd(data)
    if (sub := _str(ti.get("dir_path"))) is not None:
        cwd = cwd / sub
    return ShellCall("gemini", command, cwd, data, _str(data.get("session_id")))


def _opencode_call(data: dict[str, Any]) -> ShellCall | None:
    ti = data.get("input")
    if data.get("tool") != "shell" or not isinstance(ti, dict) or not isinstance(ti.get("command"), str):
        return None
    cwd = _session_cwd(data)
    if (workdir := _str(ti.get("workdir"))) is not None:
        cwd = cwd / workdir
    return ShellCall("opencode", ti["command"], cwd, data, _str(data.get("sessionID")),
                     _str(data.get("id")), _str(data.get("agent")))


def _pi_call(data: dict[str, Any]) -> ShellCall | None:
    ti = data.get("input")
    if data.get("toolName") != "bash" or not isinstance(ti, dict) or not isinstance(ti.get("command"), str):
        return None
    return ShellCall("pi", ti["command"], _session_cwd(data), data, _str(data.get("session_id")),
                     _str(data.get("toolCallId")))


CALLS: dict[str, Callable[[dict[str, Any]], ShellCall | None]] = {
    CLAUDE_CODE: _claude_code_call, "codex": _codex_call, "cursor": _cursor_call, "gemini": _gemini_call,
    "opencode": _opencode_call, "pi": _pi_call}


def shell_call(harness: str, data: dict[str, Any]) -> ShellCall | None:
    """The shell command in this hook input, or None when the event is not a shell call."""
    return CALLS[check_harness(harness)](data)


# ---------------------------------------------------------------------------------------------- outcomes

# The header Codex puts before a command's output in the model-facing text (codex_context.rs, response_header).
_CODEX_HEADER = re.compile(r"Chunk ID: .*|Wall time: .*|Process exited with code (-?\d+)|"
                           r"Process running with session ID .*|Original token count: .*")


def codex_outcome(data: dict[str, Any]) -> Outcome:
    """codex_hooks.md documents no exit code field: tool_response is "Tool-specific output", and local function tools
    "normally send their model-facing output". When that text starts with the header Codex's model sees ("Wall
    time: ...", "Process exited with code N", ..., "Output:"), N is the exit code. The exec_command path sends the
    output alone (codex_context.rs, post_tool_use_response): the outcome is unknown, and the run is not recorded."""
    resp = data.get("tool_response")
    if not isinstance(resp, str):
        return Outcome(None, reason="tool_response is not text", shape_ok=False)
    lines = resp.split("\n")
    end = lines.index("Output:") if "Output:" in lines else 0
    header = [_CODEX_HEADER.fullmatch(x) for x in lines[:end]]
    if not header or not all(header) or not any(x.startswith("Wall time: ") for x in lines[:end]):
        return Outcome(None, reason="no exit code: Codex sent the output without its exit code")
    codes = [int(m.group(1)) for m in header if m is not None and m.group(1) is not None]
    if not codes:
        return Outcome(None, reason="no exit code in Codex's header (the command may still be running)")
    code = codes[-1]
    return Outcome("ok" if code == 0 else "fail", code, rough_signature("\n".join(lines[end + 1:])) if code else None)


def cursor_outcome(data: dict[str, Any]) -> Outcome:
    """postToolUse: tool_output is a "JSON-stringified result payload" with exitCode and stdout (cursor_hooks.md).
    postToolUseFailure carries failure_type (error, timeout, permission_denied) and is_interrupt, and no exit code."""
    if data.get("hook_event_name") == "postToolUseFailure":
        if data.get("is_interrupt"):
            return Outcome(None, reason="interrupted")
        kind = data.get("failure_type")
        if kind == "timeout":
            return Outcome("timeout")
        if kind == "permission_denied":
            return Outcome(None, reason="denied: the command did not run")
        if kind == "error":
            return Outcome(None, reason="Cursor reports the failure without an exit code (failure_type error)")
        return Outcome(None, reason=f"unknown failure_type {kind!r}", shape_ok=False)
    raw = data.get("tool_output")
    try:
        out = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        out = None
    if not isinstance(out, dict) or not isinstance(out.get("exitCode"), int) or isinstance(out["exitCode"], bool):
        return Outcome(None, reason="tool_output has no exitCode", shape_ok=False)
    code: int = out["exitCode"]
    stdout = out.get("stdout")
    text = stdout if isinstance(stdout, str) else ""  # stderr is not documented
    return Outcome("ok" if code == 0 else "fail", code, rough_signature(text) if code else None)


# The lines gemini_shell.ts appends after "Output: <output>", in order; each is optional.
_GEMINI_TAIL = ("Error", "Exit Code", "Signal", "Background PIDs", "Process Group PGID")
_GEMINI_TIMEOUT = "Command was automatically cancelled because it exceeded the timeout"
_GEMINI_CANCELLED = "Command was cancelled by user"
_GEMINI_BACKGROUND = "Command moved to background"


def gemini_outcome(data: dict[str, Any]) -> Outcome:
    """AfterTool: tool_response has llmContent (gemini_hooks_ref.md). For run_shell_command, llmContent is
    "Output: <output>" followed by "Error: ...", "Exit Code: N" (only when N is not 0), "Signal: ...",
    "Background PIDs: ..." and "Process Group PGID: ..." lines when they apply (gemini_shell.ts). No Exit Code line,
    no Error and no Signal is exit 0."""
    ti = data.get("tool_input")
    if isinstance(ti, dict) and ti.get("is_background"):
        return Outcome(None, reason="run in the background: no result yet")
    resp = data.get("tool_response")
    text = resp.get("llmContent") if isinstance(resp, dict) else None
    if not isinstance(text, str):
        return Outcome(None, reason="tool_response has no llmContent text", shape_ok=False)
    if text.startswith(_GEMINI_TIMEOUT):
        return Outcome("timeout")
    if text.startswith((_GEMINI_CANCELLED, _GEMINI_BACKGROUND)):
        return Outcome(None, reason="cancelled, or run in the background")
    if not text.startswith("Output: "):
        return Outcome(None, reason="llmContent does not start with 'Output: ' (summarised or wrapped)",
                       shape_ok=False)
    lines = text[len("Output: "):].split("\n")
    tail: dict[str, str] = {}
    for key in reversed(_GEMINI_TAIL):
        if len(lines) > 1 and lines[-1].startswith(key + ": "):
            tail[key] = lines.pop()[len(key) + 2:]
    output = "" if lines == ["(empty)"] else "\n".join(lines)
    if "Exit Code" in tail:
        try:
            code = int(tail["Exit Code"])
        except ValueError:
            return Outcome(None, reason="unreadable Exit Code line", shape_ok=False)
        return Outcome("ok" if code == 0 else "fail", code, rough_signature(output) if code else None)
    if "Signal" in tail or "Error" in tail:
        return Outcome(None, reason="ended by a signal or an error, without an exit code")
    return Outcome("ok", 0)


def _structured_outcome(result: dict[str, Any], exit_key: str) -> Outcome:
    code = result.get(exit_key)
    if not isinstance(code, int) or isinstance(code, bool):
        return Outcome(None, reason="no integer exit code in the structured shell result", shape_ok=False)
    return Outcome("ok" if code == 0 else "fail", code,
                   rough_signature(_str(result.get("output")) or "") if code else None)


def opencode_outcome(data: dict[str, Any]) -> Outcome:
    """OpenCode V2 distinguishes completed tool calls from failures to execute the tool itself."""
    if data.get("status") == "error":
        return Outcome(None, reason="OpenCode reports an execution error without an exit code")
    result = data.get("result")
    output = result.get("output") if isinstance(result, dict) else None
    if data.get("status") != "completed" or not isinstance(output, dict):
        return Outcome(None, reason="no structured result from the OpenCode shell tool", shape_ok=False)
    if "exitCode" in output and output["exitCode"] is None:
        return Outcome(None, reason="OpenCode shell has no final exit code (background or signalled)")
    return _structured_outcome(output, "exitCode")


def pi_outcome(data: dict[str, Any]) -> Outcome:
    """Pi's bash tool reports an actual process exit code in structuredContent on both exit paths."""
    output = data.get("structuredContent")
    if not isinstance(output, dict):
        return Outcome(None, reason="Pi bash has no structured exit code", shape_ok=data.get("isError") is True)
    return _structured_outcome(output, "exit_code")


OUTCOMES: dict[str, Callable[[dict[str, Any]], Outcome]] = {
    CLAUDE_CODE: claude_code_outcome, "codex": codex_outcome, "cursor": cursor_outcome, "gemini": gemini_outcome,
    "opencode": opencode_outcome, "pi": pi_outcome}


# ---------------------------------------------------------------------------------------------- reports


def report_input(harness: str, data: dict[str, Any]) -> dict[str, Any]:
    """A subagent-stop input in the shape hooks.py reads (Claude Code's SubagentStop): agent_type,
    last_assistant_message, cwd, session_id, agent_id. The transcript paths are dropped: hooks.py reads Claude
    Code's transcript format to see whether the report cites the run's own records, and Codex says its transcript
    "isn't a stable interface for hooks". Without them every report is captured as a note."""
    if check_harness(harness) == CLAUDE_CODE:
        return data
    out = {k: v for k, v in data.items() if k not in ("transcript_path", "agent_transcript_path")}
    out["harness"] = harness
    if harness == "cursor":
        # subagentStop: subagent_type, status, summary; no cwd (workspace_roots is common to every hook)
        out.update({"hook_event_name": "SubagentStop", "agent_type": data.get("subagent_type"),
                    "last_assistant_message": data.get("summary"), "session_id": data.get("conversation_id")})
        if not data.get("cwd") and (root := _cursor_root(data)):
            out["cwd"] = root
        if data.get("cursor_version"):
            out["harness_version"] = data["cursor_version"]
    return out
