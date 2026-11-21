# benchbook hooks for Cursor

`hooks.json` runs the same hook commands as the Claude Code plugin, with `--harness cursor` so benchbook reads
Cursor's hook input. What each field means, and where it is documented, is in
[docs/harnesses.md](../../docs/harnesses.md).

## Install

```bash
uv tool install benchbook                     # `benchbook` on PATH; check with: command -v benchbook
mkdir -p .cursor && cp integrations/cursor/hooks.json .cursor/hooks.json    # this project
# or for every project: cp integrations/cursor/hooks.json ~/.cursor/hooks.json
```

If the file already exists, add the three entries under `hooks` to it instead. Cursor reloads `hooks.json` on save;
the Hooks tab in Customize and the Hooks output channel show what ran.

The project needs `.benchbook/config.toml` with `[checks]` (docs/code.md) for check runs, and the hook must reach the
same database as your shell (start Cursor from a shell where `benchbook doctor` passes, or set the
`BENCHBOOK_*` variables where Cursor gets its environment).

Reports are captured only for the agent types in `BENCHBOOK_HOOK_AGENTS` (default `benchbook-worker`). Cursor's
subagent types are `generalPurpose`, `explore`, `shell` and so on, so the default captures nothing: set
`BENCHBOOK_HOOK_AGENTS=generalPurpose` (or `*`) in Cursor's environment, or put it in front of the command in
`hooks.json` (`"command": "BENCHBOOK_HOOK_AGENTS=generalPurpose benchbook hook subagent-stop --harness cursor"`; on
Windows Cursor runs hook commands through PowerShell, where that prefix does not work).

## What is captured, compared with Claude Code

| | Claude Code | Cursor |
|---|---|---|
| check runs | `PostToolUse` and `PostToolUseFailure` on Bash; the exit code from `Exit code N` | `postToolUse` on Shell: `tool_output` is a JSON string with `exitCode` and `stdout`, recorded like Claude Code's run. `postToolUseFailure` has no exit code: a timeout is recorded as one, an error or a denied command is observed as `unrecordable` |
| reports | `SubagentStop` and `SubagentHandback`; a report citing the run's own records is not captured again | `subagentStop` (`summary`). No handback, and the subagent transcript is a different format, so every report is captured as a note and out-of-scope findings get no piece. `modified_files` is not recorded |
| completion gate | `TaskCompleted` exits 2 | not wired. Cursor documents no task-completed event; `stop` and `subagentStop` can return `followup_message`, which continues the agent with a new message (up to `loop_limit`). Whether that can serve as a gate is to be confirmed, so the manifest does not use it |

Cursor's hook failures include "no output" (`failClosed` in its documentation), so these hooks print `{}`. They always exit 0
and never block. `benchbook check run NAME` records the same attempt under any harness. `benchbook doctor` reports
Cursor versions (`cursor_version` in the hook input) whose input it could not read.

Cloud agents run project hooks in their own VM; benchbook there needs its own install and a database it can reach.
That has not been tried.
