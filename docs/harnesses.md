# Codex CLI, Cursor and Gemini CLI

The Claude Code hooks (docs/claude-code.md) have two siblings: `integrations/codex/hooks.json` and
`integrations/cursor/hooks.json`. They run the same commands, `benchbook hook bash-result` and
`benchbook hook subagent-stop`, with `--harness codex` or `--harness cursor`, which tells benchbook whose hook input
it is reading (`src/benchbook/harnesses.py`). Gemini CLI has a parser and fixtures (`--harness gemini`) but no
manifest. Install steps and the comparison with Claude Code are in each directory's README.

After the input is read, everything works as it does for Claude Code: the same matching rule against the declared
checks, the same `observed.jsonl` line (with `harness`, and `harness_version` where the input carries one), the
same spool entry and the same attempt (`via = "bash-hook"`, the rough signature from the exit code and the last
output line). A command that is not a declared check leaves nothing behind and the hook finishes within 50 ms for
every harness (`tests/test_hooks.py`). When the input does not say how the command ended, the run is observed as
`unrecordable` and not recorded: a run with no exit code is never recorded as a pass.

Every statement below about a harness's hook input is quoted from the copies of its documentation taken on
2026-11-21 (the materials' `04-sources/harness-docs/`): file and heading, then the words. Where those copies say
nothing, this page says "not documented", and benchbook does not read the field.

## How the hook learns its harness

The manifest's command carries it: `benchbook hook bash-result --harness codex` (also `--harness=codex`). Without
the option the hook reads Claude Code's input, so the Claude Code plugin is unchanged. An unknown name is a line in
`hook.log`; the hook still exits 0. Telling harnesses apart by the shape of the input was not chosen: Codex and
Claude Code both name the shell tool `Bash` and send `tool_input.command`, and a guess would read one as the other.

## Codex CLI

**Manifest.** codex_hooks.md, "Where Codex looks for hooks": "Local Codex looks for `hooks.json` in the locations
below", with "User | `$CODEX_HOME/hooks.json`" and "Trusted project | `.codex/hooks.json`". "Config shape": "Hooks
are organized in three levels" (a hook event, "A matcher group that decides when that event matches", "One or more
hook handlers that run when the matcher group matches"); "`timeout` is in seconds"; "Commands run with the session `cwd` as their working directory".
"Review and trust hooks": "Before a non-managed hook can run, Codex requires you to review and trust the exact hook
definition."

**Shell result.** codex_hooks.md, "Tool coverage": "Shell commands | Yes | Yes | Match as `Bash`." and "Unified exec
(`exec_command`) | Yes | Yes | Match as `Bash`." "PostToolUse": "For Bash, it also runs after commands that exit
with a non-zero status." Its fields: "`tool_name` | `string` | Canonical hook tool name, such as `Bash`",
"`tool_use_id` | `string` | Tool-call id for this invocation", "`tool_input` | `JSON value` | Tool-specific input.
`Bash` and `apply_patch` use `tool_input.command`", "`tool_response` | `JSON value` | Tool-specific output. MCP
tools send the MCP call result. Other local function tools normally send their model-facing output." "Common input
fields": "`session_id` | `string` | Current Codex session id", "`cwd` | `string` | Working directory for the
session".

- Exit code: not documented as a field. The model-facing text of a command starts with a header that
  codex_context.rs (`ExecCommandToolOutput::response_header`) builds as `Wall time: ... seconds`, `Process exited
  with code {exit_code}` (when there is one), and `Output:` before the output. benchbook reads the exit code only
  from a `tool_response` string that starts with that header. The same file's `post_tool_use_response` for
  `exec_command` returns `self.truncated_output_with_policy(...)`, the output alone: those runs, and any other
  `tool_response` without the header, are observed as `unrecordable` ("Codex sent the output without its exit
  code"). Whether the classic shell path sends the header is not shown in these copies; expect most Codex check runs
  to be observed and not recorded, and use `benchbook check run NAME` there.
- Duration, agent type of the shell call, timeouts: not documented for `PostToolUse` (recorded as 0 and empty).
- Version: not in the hook input (the common fields are `session_id`, `transcript_path`, `cwd`,
  `hook_event_name`, `model`), so doctor has no version to compare.

**Subagent report.** codex_hooks.md, "SubagentStop": "`matcher` is applied to `agent_type` for this event", fields
"`agent_id`", "`agent_type` | `string` | Subagent type or profile", "`agent_transcript_path`",
"`last_assistant_message` | `string \| null` | Latest subagent assistant message, if available". "`SubagentStop`
expects JSON on `stdout` when it exits `0`. Plain text output is invalid"; "Common output fields": "Exit `0` with no
output is treated as success and Codex continues." The hook prints nothing. "Common input fields": "the transcript
format isn't a stable interface for hooks and may change over time", so benchbook drops the transcript paths: it
does not check whether the report cites the run's own records, and captures every report as a note.

**Completion gate: not wired.** No task-completed event is documented. "SubagentStop": "To ask Codex to continue the
subagent flow, return:" `decision: "block"`; "Stop": "`decision: "block"` doesn't reject the turn. Instead,
it tells Codex to continue". Whether either can hold a piece open the way TaskCompleted does is to be confirmed.

## Cursor

**Manifest.** cursor_hooks.md, "Quickstart": "You can create it at the project level (`<project>/.cursor/hooks.json`)
or in your home directory (`~/.cursor/hooks.json`)", with `"version": 1` and `"hooks"`. "Global Configuration
Options": "`version` | number | required". "Per-Script Configuration Options": "`command` | string | required",
"`timeout` | number | platform default | Execution timeout in seconds", "`matcher` | string | - | Regex that filters
when the hook runs". "Matcher Configuration", for preToolUse / postToolUse / postToolUseFailure: "Filter by tool type. Values include
`Shell`". "Configuration", for user hooks (`~/.cursor/hooks.json`): "Run from `~/.cursor/`".

**Shell result.** cursor_hooks.md, "postToolUse": "Called after successful tool execution", example input
`"tool_name": "Shell"`, `"tool_input": { "command": "npm test" }`,
`"tool_output": "{\"exitCode\":0,\"stdout\":\"All tests passed\"}"`, `"tool_use_id"`, `"cwd"`, `"duration": 5432`;
"`tool_output` | string | JSON-stringified result payload from the tool (not raw terminal text)", "`duration` |
number | Execution time in milliseconds". "postToolUseFailure": "Called when a tool fails, times out, or is denied",
"`failure_type` | string | Type of failure: `"error"`, `"timeout"`, or `"permission_denied"`", "`is_interrupt` |
boolean | Whether this failure was caused by a user interrupt/cancellation".

- Exit code: `exitCode` in the parsed `tool_output`; the last output line from its `stdout`. A `stderr` key is not
  documented and not read.
- Whether a non-zero exit fires `postToolUse` (with `exitCode` N) or `postToolUseFailure`: not documented, so both
  are wired. `postToolUseFailure` carries no exit code: `timeout` is recorded as a timeout, `error` and
  `permission_denied` are observed as `unrecordable`, an interrupt likewise.
- Working directory: `cwd`; `tool_input.working_directory` when present (it appears in "preToolUse"'s example
  input, `{ "command": "npm install", "working_directory": "/project" }`); else `workspace_roots[0]`.
- Session: "Common schema": "`conversation_id` | string | Stable ID of the conversation across many turns".
- Version: "Common schema": "`cursor_version` | string | Cursor application version (e.g. "1.7.2")". It is kept on
  each observation, and doctor names it (below).
- "afterShellExecution" has `command`, `output` and `duration` but no exit code and no working directory, so it is
  not used.
- Output: "Per-Script Configuration Options", `failClosed`: "hook failures (crash, timeout, non-zero exit code, no
  output)". The hook prints `{}` so that "no output" is never read as a failure.

**Subagent report.** cursor_hooks.md, "subagentStop": "Called when a subagent completes, errors, or is aborted",
"`subagent_type` | string | Type of subagent: `generalPurpose`, `explore`, `shell`, etc.", "`summary` | string |
Output summary from the subagent", "`modified_files` | string[] | Files the subagent modified". The summary is the
report and the subagent type is the agent type `BENCHBOOK_HOOK_AGENTS` filters on. The input has no `cwd`;
"Common schema": "`workspace_roots` | string[] | The list of root folders in the workspace" supplies it.
`modified_files` is not recorded. "agent_transcript_path" points to "the subagent's own transcript file", a format
benchbook does not read, so every report is captured as a note.

**Completion gate: not wired.** No task-completed event is documented. "subagentStop": "`followup_message` |
string (optional) | Auto-continue with this message. Only consumed when `status` is `"completed"`"; "stop" has the
same field. Whether a follow-up can hold a piece open is to be confirmed.

## Gemini CLI (parser only)

gemini_hooks_ref.md, "Configuration schema": "Hooks are defined in `settings.json` within the `hooks` object"; no
manifest is shipped (the spec asks for Codex and Cursor). "Matchers and tool names": "Built-in Tools: You can match
any built-in tool (for example, `read_file`, `run_shell_command`)". "`AfterTool`": "`tool_input`: (`object`) The
original arguments", "`tool_response`: (`object`) The result containing `llmContent`, `returnDisplay`, and
optional `error`". "Base input schema": `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `timestamp`.
"Global hook mechanics": "`0`: Success. `stdout` is parsed as JSON" (the hook prints `{}`).

- Exit code: gemini_shell.ts builds `llmContent` as `Output: ${result.output || '(empty)'}`, then
  `Error: ...` when there was an error, `Exit Code: ${result.exitCode}` "if (result.exitCode !== null &&
  result.exitCode !== 0)", `Signal: ...`, `Background PIDs: ...` and `Process Group PGID: ...`. benchbook peels
  those lines off the end: `Exit Code: N` is a failure with N, no Exit Code, Error or Signal line is exit 0, and an
  Error or Signal line without an exit code is `unrecordable`. "Command was automatically cancelled because it
  exceeded the timeout" is a timeout; a user cancel or `is_background` is `unrecordable`.
- `llmContent` goes through `wrapUntrusted(llmContent, outputSource)` and, with a summarizer configured, is a
  summary; neither format is in these copies. Text that does not start with `Output: ` is `unrecordable` and counts
  as an unexpected shape for doctor.
- Working directory: the session `cwd`, joined with `tool_input.dir_path` when given (gemini_shell.ts resolves
  `this.params.dir_path` against the target directory).
- Tool call id, duration, version: not documented.
- Subagent reports: spec §7 says Gemini has no dedicated event; not wired.

## Doctor

`benchbook doctor` adds a `harness hook input` line once a Codex, Cursor or Gemini hook has observed a declared
check run in the last 24 hours (for this shell's database). It fails when some of those inputs were not shaped as
the documentation and the stored fixtures say (Cursor's `tool_output` without an integer `exitCode`, an unknown
`failure_type`, a Codex `tool_response` that is not text, a Gemini `llmContent` that does not start with
`Output: `), and names the harness version that sent them next to the fixtures' version: only Cursor's input carries
one (`cursor_version`; the fixtures are of 1.7.2, the documentation's example). Codex and Gemini lines say "no
version in its input". A Codex output without its exit code is the documented shape and does not count.

## Fixtures

`tests/fixtures/hooks/<harness>/` holds each harness's input for the run the Claude Code fixtures
(`post_tool_use_bash.json`, `post_tool_use_failure_bash.json`) describe: the same command and the same output,
so `tests/test_harness_hooks.py` can check that each yields the same attempt (piece, check, outcome, signature,
via, commit, author; the duration only where the harness documents one). Fields benchbook does not read keep the
documentation's example values or a placeholder.

| fixture | built from |
|---|---|
| `codex/post_tool_use_bash.json`, `codex/post_tool_use_bash_failure.json` | codex_hooks.md "Common input fields" and "PostToolUse" (field tables), the "SessionEnd" example's `session_id`/`transcript_path`; `tool_response` in the model-facing format of codex_context.rs `response_text` |
| `codex/post_tool_use_exec_command.json` | the same, with `tool_response` the output alone (codex_context.rs `post_tool_use_response`) |
| `codex/subagent_stop.json` | codex_hooks.md "SubagentStop" (field table) |
| `cursor/post_tool_use_shell.json`, `cursor/post_tool_use_shell_failure.json` | cursor_hooks.md "postToolUse" example input and the "Common schema" input (`cursor_version` "1.7.2" from its table) |
| `cursor/post_tool_use_failure_shell.json` | cursor_hooks.md "postToolUseFailure" example input |
| `cursor/subagent_stop.json` | cursor_hooks.md "subagentStop" example input and the "Common schema" input |
| `gemini/after_tool_shell.json`, `gemini/after_tool_shell_failure.json` | gemini_hooks_ref.md "Base input schema" and "`AfterTool`"; `llmContent` as gemini_shell.ts builds it |

When a harness changes its hook input, add a fixture of the new shape next to the old one, teach its parser in
`harnesses.py`, and update `FIXTURE_VERSIONS` when the new fixture carries a version.
