# Claude Code

## Install as a plugin

```bash
uv tool install --from /path/to/assay assay      # puts `assay` on PATH (the plugin runs `assay mcp`, `assay hook`)
claude plugin marketplace add /path/to/assay     # the repository root has .claude-plugin/marketplace.json
claude plugin install assay@assay
```

The plugin (`integrations/claude-code/`) contains:

| file | what |
|---|---|
| `.mcp.json` | the `assay` MCP server (`assay mcp`, stdio) |
| `agents/assay-worker.md` | worker subagent: worker tools only (no `assay_decide`, `assay_create_tasks`, `assay_cancel_task`) |
| `skills/assay/SKILL.md` | rules for any agent using the ledger |
| `skills/assay-lead/SKILL.md` | the lead loop, `/assay:assay-lead <goal>` |
| `hooks/hooks.json` | `SubagentStop` and `PostToolUse(SubagentHandback)` -> `assay hook ...` |

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

## The result-capture hook

When a subagent stops, `assay hook subagent-stop` reads its final message. If the message cites an existing
experiment, claim or task id, nothing happens. Otherwise a `note` claim tagged `auto-captured` is recorded with the
first 300 characters as its statement and the first 4,000 in `payload.text`. The hook only acts in directories
with `.assay/config.toml`, returns immediately (the work happens in a detached process) and always exits 0;
problems go to `~/.local/state/assay/hook.log`.

## Other MCP clients (Codex, ...)

See `integrations/agents-md/AGENTS.md`: the same rules, runtime-neutral, and an MCP config example.
