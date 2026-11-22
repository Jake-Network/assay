# benchbook hooks for Codex CLI

`hooks.json` runs the same hook commands as the Claude Code plugin, with `--harness codex` so benchbook reads
Codex's hook input. What each field means, and where it is documented, is in
[docs/harnesses.md](../../docs/harnesses.md).

## Install

```bash
uv tool install benchbook                     # `benchbook` on PATH; check with: command -v benchbook
mkdir -p .codex && cp integrations/codex/hooks.json .codex/hooks.json       # this (trusted) project
# or for every project: cp integrations/codex/hooks.json "${CODEX_HOME:-$HOME/.codex}/hooks.json"
```

If the file already exists, merge the entries under `hooks` (Codex runs every matching hook from
every file, so the same command in two files runs twice). Codex runs a non-managed hook only after you review and
trust it: open `/hooks` in the Codex CLI and trust the benchbook hooks. Codex loads project hooks only from a trusted project.

The project needs `.benchbook/config.toml` with `[checks]` (docs/code.md) for check runs, and the hook must reach the
same database as your shell: which environment Codex gives hook commands is not documented, so start Codex from a
shell where `benchbook doctor` passes.

Reports are captured only for the agent types in `BENCHBOOK_HOOK_AGENTS` (default `benchbook-worker`). Codex's
`agent_type` is the "Subagent type or profile"; set `BENCHBOOK_HOOK_AGENTS` to the one your workers run as, or `*`.

## What is captured, compared with Claude Code

| | Claude Code | Codex |
|---|---|---|
| boundary context | `SessionStart` brief, including after compaction | `SessionStart` brief, including after compaction, and pending notes on `PostToolUse` |
| check runs | `PostToolUse` and `PostToolUseFailure` on Bash; the exit code from `Exit code N` | `PostToolUse` on Bash, which also fires after a non-zero exit. Codex documents no exit code field: the run is recorded only when `tool_response` starts with the header Codex shows its model (`Process exited with code N`, then `Output:`). The `exec_command` path sends the output alone, so those runs are observed as `unrecordable` and not recorded |
| reports | `SubagentStop` and `SubagentHandback`; a report citing the run's own records is not captured again | `SubagentStop` (`last_assistant_message`). No handback. Codex's transcript "isn't a stable interface for hooks", so benchbook does not read it: every report is captured as a note, cited records or not, and out-of-scope findings get no piece |
| completion gate | `TaskCompleted` exits 2 | not wired. Codex documents no task-completed event; `Stop` and `SubagentStop` can return `decision: "block"` (or exit 2), which makes Codex continue the turn or the subagent. Whether that can serve as a gate is to be confirmed, so the manifest does not use it |
| duration, agent type of a check run | from the hook input | not documented for `PostToolUse`: recorded as 0 and empty |

`benchbook check run NAME` records the same attempt with its exit code under any harness; use it where the hook
cannot read one. `benchbook doctor` reports Codex hook inputs it could not read (Codex's input carries no version
to compare).
