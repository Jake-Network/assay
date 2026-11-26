# Benchbook hooks for Cursor

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook
benchbook install --no-cli --harness cursor    # merges hooks.json into ~/.cursor/hooks.json
```

To install in one project only, merge the `hooks` object from [hooks.json](hooks.json) into `.cursor/hooks.json`.
Cursor reloads `hooks.json` on save; the Hooks tab in Customize and the Hooks output channel show what ran.

Run `benchbook init` in the project, and start Cursor from a shell where `benchbook doctor` passes, or set the
`BENCHBOOK_*` variables where Cursor gets its environment. `sessionStart` loads the scoped brief. `postToolUse` and
`postToolUseFailure` deliver pending notes and record declared Shell checks (a failure without an exit code is
recorded only when it is a timeout). The completion gate is not wired.

`subagentStop` captures reports as notes for the agent types in `BENCHBOOK_HOOK_AGENTS`. Cursor's types are
`generalPurpose`, `explore`, `shell` and so on, so the default `benchbook-worker` captures nothing: set
`BENCHBOOK_HOOK_AGENTS=generalPurpose` (or `*`) in Cursor's environment, or put it in front of the command in
`hooks.json` (not on Windows, where Cursor runs hook commands through PowerShell):

```json
"command": "BENCHBOOK_HOOK_AGENTS=generalPurpose benchbook hook subagent-stop --harness cursor"
```

Cloud agents run project hooks in their own VM, where benchbook needs its own install and a database it can reach;
this is untested.

[Hook fields and fixture provenance](../../docs/harnesses.md#cursor)
