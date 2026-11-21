# Claude Code

## Install as a plugin

```bash
uv tool install --from /path/to/benchbook benchbook      # puts `benchbook` on PATH (the plugin runs `benchbook mcp`, `benchbook hook`)
claude plugin marketplace add /path/to/benchbook     # the repository root has .claude-plugin/marketplace.json
claude plugin install benchbook@benchbook
```

Claude Code starts `benchbook mcp` and `benchbook hook` from its own PATH. If `command -v benchbook` finds nothing in a new
terminal, run `uv tool update-shell` once (it adds `~/.local/bin` to your shell profile), open a new terminal and
restart Claude Code. Without it the MCP server does not connect and the hook fails with exit 127 at every subagent
stop. Run `benchbook doctor` in the project directory: it prints the installed build (`benchbook --version`), the database
schema revision and the newest one this build knows, and checks what Claude Code needs. Each failing check prints the
command that fixes it:

| check | fails when |
|---|---|
| `benchbook on PATH` | `benchbook` is not on the PATH Claude Code inherits (or the `PATH` in its settings `env`) |
| `claude code plugin` | `benchbook@benchbook` is not installed or not enabled (a manual install with `.mcp.json` passes) |
| `plugin hooks` | the installed plugin predates 0.1.1 and still runs the hook only for `benchbook-worker` |
| `BENCHBOOK_DATABASE_URL`, `BENCHBOOK_DATA_DIR`, `BENCHBOOK_PROJECT` | the shell value selects a different database or project than the `env` in `~/.claude/settings.json`, `.claude/settings.json` or `.claude/settings.local.json` (the URL itself is never printed) |
| `hook failures` | for this shell's database: `hook.log` has failure lines from the last 24 hours (after the last clean `benchbook hook replay` of its spool), or its spool holds reports in `dead/` or queued for over 5 minutes |

Claude Code keeps an installed plugin version until you update it. After pulling a new benchbook, update the plugin as
well and restart Claude Code:

```bash
claude plugin marketplace update benchbook && claude plugin update benchbook@benchbook
```

Bump `version` in `integrations/claude-code/.claude-plugin/plugin.json` whenever a file under
`integrations/claude-code/` changes; an install of the same version does not pick the change up.

On macOS the embedded database lives in `~/Library/Application Support/benchbook/pg`. To keep it elsewhere set
`BENCHBOOK_DATA_DIR`, and set it in Claude Code's `settings.json` `env` too: the CLI, MCP server and hook must see the
same value.

The plugin (`integrations/claude-code/`) contains:

| file | what |
|---|---|
| `.mcp.json` | the `benchbook` MCP server (`benchbook mcp`, stdio) |
| `agents/benchbook-worker.md` | worker subagent: worker tools only (no `benchbook_decide`, `benchbook_create_tasks`, `benchbook_cancel_task`) |
| `skills/benchbook/SKILL.md` | rules for any agent using the ledger |
| `skills/benchbook-lead/SKILL.md` | the lead loop, `/benchbook:benchbook-lead <goal>` |
| `hooks/hooks.json` | `SubagentStop` (every subagent; `BENCHBOOK_HOOK_AGENTS` picks which are captured) and `PostToolUse(SubagentHandback)` -> `benchbook hook ...` |

For development, `claude --plugin-dir integrations/claude-code` loads it without installing.

## Install by hand (no plugin)

```bash
cp integrations/claude-code/manual/.mcp.json .mcp.json
mkdir -p .claude/agents .claude/skills
cp integrations/claude-code/manual/agents/benchbook-worker.md .claude/agents/
cp -r integrations/claude-code/skills/benchbook integrations/claude-code/skills/benchbook-lead .claude/skills/
```

Merge `integrations/claude-code/manual/settings.hooks.json` into `.claude/settings.json`. The skill is then
`/benchbook-lead`. Tool names differ between the two installs (`mcp__plugin_benchbook_benchbook__*` vs `mcp__benchbook__*`), which is
why the manual worker file is separate.

## The loop

1. `benchbook init` (or the lead does it): goal, metric, direction. `lead` is the orchestrator, `human` is you.
2. The lead creates 4-8 tasks with `spec.config_space` and tags.
3. It launches K `benchbook-worker` subagents with a two-sentence prompt: their agent name and "take a task".
4. Workers: `benchbook_next_task` -> `benchbook_brief` -> for each evaluation `benchbook_check_tried` -> run ->
   `benchbook_record_experiment` -> claims with evidence -> `benchbook_complete_task`; final message <= 3 sentences + ids.
5. The lead reads `benchbook_status` and `benchbook_brief` (and `benchbook_sql`), decides claims, plans the next round.

You can decide as the human from a terminal: `benchbook decide c:1a2b3c4d verified --oracle human --rationale "..." --evidence e:...,e:... --as human`.

The stdio MCP server starts and lists tools before a project exists. If the lead runs `benchbook init` inside an
open session, its next ledger call opens the newly initialized project.

## The result-capture hook

When an `benchbook-worker` subagent stops, `benchbook hook subagent-stop` reads its final message. A report sent through
`SubagentHandback` is captured from the handback. A cited experiment
or claim ID counts when the subagent's transcript shows a successful write returning that ID and the ledger
timestamps place the record within the run. The hook captures the report when the transcript is missing or
the citation cannot establish that the run wrote the record. A `note` claim tagged `auto-captured` keeps the first
300 characters as its statement and the first 4,000 in `payload.text`. Identical message bodies merge across
hook events. Reports with a shared opening and different conclusions each get a record.

Other subagents (Explore, general-purpose, ...) are not captured. The hook fires for every subagent and
`BENCHBOOK_HOOK_AGENTS` (comma separated, default `benchbook-worker`, with or without a plugin prefix) decides which are
captured (`assay-worker`, the worker's name before the rename, counts as `benchbook-worker`); any other type exits at once without reading the config or touching the database. An event without an
agent type (older Claude Code versions) is not captured either; `BENCHBOOK_HOOK_AGENTS=*` captures every event. To capture your own
worker types too, set `BENCHBOOK_HOOK_AGENTS=benchbook-worker,implementer` in your shell and in the `env` of Claude Code's
`settings.json`. This works the same for the plugin and the manual install.

The hook uses the project selected by `.benchbook/config.toml` or `BENCHBOOK_PROJECT`; when it finds neither (for example a
worktree without a committed `.benchbook/`) it writes a "no benchbook project" line to the log. It writes the report to a
local spool and exits 0 while a detached process handles the database write; only one processor drains a spool at a
time. The processor retries connection failures for about a minute and keeps those reports for the next hook.
Failures a retry cannot fix (the project does not exist in this database, the schema is newer than this benchbook) move
the report to the spool's `dead/` folder. Malformed spool files are quarantined with the `.invalid` suffix.
After an outage or a fix, `benchbook hook replay` retries pending and dead reports once in the foreground using
the same database settings. A report queued by a build before this one does not record which
`BENCHBOOK_HOOK_AGENTS` accepted it; if the current setting does not cover its agent type, it moves to `dead/` instead of
being dropped, and `BENCHBOOK_HOOK_AGENTS=<the original list> benchbook hook replay` captures it. See `~/.local/state/benchbook/hook.log` for capture and retry status.

Earlier builds placed spool files directly in `~/.local/state/benchbook/spool/` without recording the database target.
To recover one, use the environment and project settings from the original session and run
`python -m benchbook.hooks /path/to/spooled-entry.json` with the Python environment that has benchbook installed.

## Installs from before the rename (assay)

benchbook was called assay before v0.1.0. An install from then keeps working during 0.1.x with nothing changed,
and nothing is moved or rewritten for you:

| what the install has | what benchbook does with it |
|---|---|
| `ASSAY_*` variables (shell, `env` in Claude Code's `settings.json`) | read when the `BENCHBOOK_*` name is unset. One line on stderr per process says so; a hook writes it to `hook.log` instead |
| `.assay/config.toml` | found by the same search (parent directories, the main worktree); `.benchbook/` wins in the same directory. `benchbook init` and `benchbook ar` keep writing into `.assay/` while it is the only folder |
| `~/.local/share/assay/pg` (macOS: `~/Library/Application Support/assay/pg`) | stays the embedded database while the new location holds no cluster. The database in it keeps the name `assay` |
| `~/.local/state/assay` | stays the state directory while the new one does not exist, so reports queued before the upgrade are replayed |
| the `assay` command | runs as `benchbook` after one notice line on stderr (`hook.log` for a hook) |
| the plugin `assay@assay` | its hooks call `assay hook ...` and are captured as before (`assay:assay-worker` is a worker); `assay mcp` serves the tools under their old `assay_*` names (its instructions and briefs name them so too), which is what that plugin's worker and skills call |
| `assay://blob/<sha256>` in recorded artifacts | read for as long as the ledger exists; new records say `benchbook://blob/` |

`benchbook doctor` lists what it found under the old name, each with the command that moves it:

```bash
git mv .assay .benchbook                         # in each repository (plain mv when the folder is not committed)
benchbook db stop && mkdir -p ~/.local/share/benchbook && mv ~/.local/share/assay/pg ~/.local/share/benchbook/pg
mv ~/.local/state/assay ~/.local/state/benchbook   # with no Claude Code session running
claude plugin marketplace add Jake-Network/benchbook && claude plugin install benchbook@benchbook   # then remove assay@assay
```

Rename `ASSAY_*` variables to `BENCHBOOK_*` in your shell profile and in `settings.json` together: the CLI, the MCP
server and the hook must still see the same database. The order of the steps does not matter, and each can wait.

## Other MCP clients (Codex, ...)

See `integrations/agents-md/AGENTS.md`: the same rules, runtime-neutral, and an MCP config example.
