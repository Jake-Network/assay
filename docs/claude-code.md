# Claude Code

## Install as a plugin

```bash
uv tool install --from /path/to/assay assay      # puts `assay` on PATH (the plugin runs `assay mcp`, `assay hook`)
claude plugin marketplace add /path/to/assay     # the repository root has .claude-plugin/marketplace.json
claude plugin install assay@assay
```

Claude Code starts `assay mcp` and `assay hook` from its own PATH. If `command -v assay` finds nothing in a new
terminal, run `uv tool update-shell` once (it adds `~/.local/bin` to your shell profile), open a new terminal and
restart Claude Code. Without it the MCP server does not connect and the hook fails with exit 127 at every subagent
stop. Run `assay doctor` in the project directory: it prints the installed build (`assay --version`), the database
schema revision and the newest one this build knows, and checks what Claude Code needs. Each failing check prints the
command that fixes it:

| check | fails when |
|---|---|
| `assay on PATH` | `assay` is not on the PATH Claude Code inherits (or the `PATH` in its settings `env`) |
| `claude code plugin` | `assay@assay` is not installed or not enabled (a manual install with `.mcp.json` passes) |
| `ASSAY_DATA_DIR`, `ASSAY_PROJECT` | the shell value differs from the `env` in `~/.claude/settings.json`, `.claude/settings.json` or `.claude/settings.local.json` |
| `hook failures` | `hook.log` has failure lines from the last 24 hours (after the last clean `assay hook replay`), or the spool's `dead/` holds reports |

On macOS the embedded database lives in `~/Library/Application Support/assay/pg`. To keep it elsewhere set
`ASSAY_DATA_DIR`, and set it in Claude Code's `settings.json` `env` too: the CLI, MCP server and hook must see the
same value.

The plugin (`integrations/claude-code/`) contains:

| file | what |
|---|---|
| `.mcp.json` | the `assay` MCP server (`assay mcp`, stdio) |
| `agents/assay-worker.md` | worker subagent: worker tools only (no `assay_decide`, `assay_create_tasks`, `assay_cancel_task`) |
| `skills/assay/SKILL.md` | rules for any agent using the ledger |
| `skills/assay-lead/SKILL.md` | the lead loop, `/assay:assay-lead <goal>` |
| `hooks/hooks.json` | `SubagentStop` (every subagent; `ASSAY_HOOK_AGENTS` picks which are captured) and `PostToolUse(SubagentHandback)` -> `assay hook ...` |

For development, `claude --plugin-dir integrations/claude-code` loads it without installing.

## Install by hand (no plugin)

```bash
cp integrations/claude-code/manual/.mcp.json .mcp.json
mkdir -p .claude/agents .claude/skills
cp integrations/claude-code/manual/agents/assay-worker.md .claude/agents/
cp -r integrations/claude-code/skills/assay integrations/claude-code/skills/assay-lead .claude/skills/
```

Merge `integrations/claude-code/manual/settings.hooks.json` into `.claude/settings.json`. The skill is then
`/assay-lead`. Tool names differ between the two installs (`mcp__plugin_assay_assay__*` vs `mcp__assay__*`), which is
why the manual worker file is separate.

## The loop

1. `assay init` (or the lead does it): goal, metric, direction. `lead` is the orchestrator, `human` is you.
2. The lead creates 4-8 tasks with `spec.config_space` and tags.
3. It launches K `assay-worker` subagents with a two-sentence prompt: their agent name and "take a task".
4. Workers: `assay_next_task` -> `assay_brief` -> for each evaluation `assay_check_tried` -> run ->
   `assay_record_experiment` -> claims with evidence -> `assay_complete_task`; final message <= 3 sentences + ids.
5. The lead reads `assay_status` and `assay_brief` (and `assay_sql`), decides claims, plans the next round.

You can decide as the human from a terminal: `assay decide c:1a2b3c4d verified --oracle human --rationale "..." --evidence e:...,e:... --as human`.

The stdio MCP server starts and lists tools before a project exists. If the lead runs `assay init` inside an
open session, its next ledger call opens the newly initialized project.

## The result-capture hook

When an `assay-worker` subagent stops, `assay hook subagent-stop` reads its final message. A report sent through
`SubagentHandback` is captured from the handback. A cited experiment
or claim ID counts when the subagent's transcript shows a successful write returning that ID and the ledger
timestamps place the record within the run. The hook captures the report when the transcript is missing or
the citation cannot establish that the run wrote the record. A `note` claim tagged `auto-captured` keeps the first
300 characters as its statement and the first 4,000 in `payload.text`. Identical message bodies merge across
hook events. Reports with a shared opening and different conclusions each get a record.

Other subagents (Explore, general-purpose, ...) are not captured. The hook fires for every subagent and
`ASSAY_HOOK_AGENTS` (comma separated, default `assay-worker`, with or without a plugin prefix) decides which are
captured; any other type exits at once without reading the config or touching the database. To capture your own
worker types too, set `ASSAY_HOOK_AGENTS=assay-worker,implementer` in your shell and in the `env` of Claude Code's
`settings.json`. This works the same for the plugin and the manual install.

The hook uses the project selected by `.assay/config.toml` or `ASSAY_PROJECT`; when it finds neither (for example a
worktree without a committed `.assay/`) it writes a "no assay project" line to the log. It writes the report to a
local spool and exits 0 while a detached process handles the database write; only one processor drains a spool at a
time. The processor retries connection failures for about a minute and keeps those reports for the next hook.
Failures a retry cannot fix (the project does not exist in this database, the schema is newer than this assay) move
the report to the spool's `dead/` folder. Malformed spool files are quarantined with the `.invalid` suffix.
After an outage or a fix, `assay hook replay` retries pending and dead reports once in the foreground using
the same database settings. See `~/.local/state/assay/hook.log` for capture and retry status.

Earlier builds placed spool files directly in `~/.local/state/assay/spool/` without recording the database target.
To recover one, use the environment and project settings from the original session and run
`python -m assay.hooks /path/to/spooled-entry.json` with the Python environment that has assay installed.

## Other MCP clients (Codex, ...)

See `integrations/agents-md/AGENTS.md`: the same rules, runtime-neutral, and an MCP config example.
