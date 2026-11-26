# Benchbook hooks for Codex CLI

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook
benchbook install --no-cli --harness codex     # merges hooks.json into $CODEX_HOME/hooks.json (default ~/.codex)
```

To install in one trusted project only, merge the `hooks` object from [hooks.json](hooks.json) into
`.codex/hooks.json`. Install at one level: Codex runs every matching hook from every file, so the same command in two
files runs twice. Codex runs a non-managed hook only after you trust it: open `/hooks` in the Codex CLI and trust the
benchbook hooks.

Run `benchbook init` in the project, and start Codex from a shell where `benchbook doctor` passes (which environment
Codex gives hook commands is not documented). `SessionStart` loads the scoped brief, also after compaction.
`PostToolUse` delivers pending notes and records declared Bash checks when the output carries Codex's exit-code
header; the `exec_command` path sends the output alone, so use `benchbook check run NAME` there. `SubagentStop`
captures reports as notes for the agent types in `BENCHBOOK_HOOK_AGENTS` (Codex's "Subagent type or profile"; default
`benchbook-worker`, or `*`). The completion gate is not wired.

[Hook fields and fixture provenance](../../docs/harnesses.md#codex-cli)
