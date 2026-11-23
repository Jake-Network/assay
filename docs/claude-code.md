# Claude Code

## Install as a plugin

```bash
uv tool install --from /path/to/benchbook benchbook      # puts `benchbook` on PATH (the plugin runs `benchbook mcp`, `benchbook hook`)
claude plugin marketplace add /path/to/benchbook     # the repository root has .claude-plugin/marketplace.json
claude plugin install benchbook@benchbook            # hooks only: no MCP tools in the session
claude plugin install benchbook-mcp@benchbook        # optional: the MCP server, worker subagent and lead skill
```

`benchbook install` does the same in one line (`uv tool install benchbook==<its version>`, the marketplace, the hook
plugin; `--mcp` adds benchbook-mcp, `--dry-run` prints the steps, `--harness codex|cursor` merges that harness's
manifest instead). `benchbook uninstall` removes the plugins, the state directory (spool, logs, notes), this
checkout's `.benchbook/` when `git ls-files` answers that it tracks nothing there (a failing git or a corrupt index
keeps the folder) and its `.git/info/exclude` line. `--purge` also removes the embedded database when its directory
contains the PostgreSQL version, data and control files and `pg_ctl status` confirms that its server has stopped.

Claude Code loads a plugin's MCP server whenever the plugin is enabled, and every tool definition is in every
prompt. That is why the hooks and the MCP server are two plugins: the hook plugin costs no tokens per turn.

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
| `plugin hooks` | the installed plugin predates 0.1.1 and still runs the hook only for `benchbook-worker`, or predates 0.1.3 and runs neither `benchbook hook bash-result` nor `benchbook hook task-completed` |
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

The hook plugin (`integrations/claude-code/`, `benchbook@benchbook`) contains:

| file | what |
|---|---|
| `bin/benchbook-hook` | the shim every hook runs: `benchbook hook EVENT` from PATH (`assay hook` when only that is there); without either it notes one line in `${CLAUDE_PLUGIN_DATA}/missing-cli`, which `benchbook doctor` reports, and exits 0 |
| `hooks/hooks.json` | `SubagentStop` (every subagent; `BENCHBOOK_HOOK_AGENTS` picks which are captured) and `PostToolUse(SubagentHandback)` -> `benchbook hook ...`; `PostToolUse(Bash)` and `PostToolUseFailure(Bash)` -> `benchbook hook bash-result`; `TaskCompleted` -> `benchbook hook task-completed` (see below) |

Every command in hooks.json is `"${CLAUDE_PLUGIN_ROOT}/bin/benchbook-hook" EVENT`, so the plugin calls only a file of
its own (the directory's submission checklist asks for that).

The MCP plugin (`integrations/claude-code-mcp/`, `benchbook-mcp@benchbook`) contains:

| file | what |
|---|---|
| `.mcp.json` | the `benchbook` MCP server (`benchbook mcp`, stdio) |
| `agents/benchbook-worker.md` | worker subagent: worker tools only (no `benchbook_decide`, `benchbook_create_tasks`, `benchbook_cancel_task`) |
| `skills/benchbook/SKILL.md` | rules for any agent using the ledger |
| `skills/benchbook-lead/SKILL.md` | the lead loop, `/benchbook-mcp:benchbook-lead <goal>` |

For development, `claude --plugin-dir integrations/claude-code --plugin-dir integrations/claude-code-mcp` loads both
without installing.

## Install by hand (no plugin)

```bash
cp integrations/claude-code/manual/.mcp.json .mcp.json
mkdir -p .claude/agents .claude/skills
cp integrations/claude-code/manual/agents/benchbook-worker.md .claude/agents/
cp -r integrations/claude-code-mcp/skills/benchbook integrations/claude-code-mcp/skills/benchbook-lead .claude/skills/
```

Merge `integrations/claude-code/manual/settings.hooks.json` into `.claude/settings.json`. The skill is then
`/benchbook-lead`. Tool names differ between the two installs (`mcp__plugin_benchbook-mcp_benchbook__*` vs `mcp__benchbook__*`), which is
why the manual worker file is separate.

## The loop

1. `benchbook init` (or the lead does it): goal, metric, direction. `lead` is the orchestrator, `human` is you.
2. The lead creates 4-8 tasks with `spec.config_space` and tags.
3. It launches K `benchbook-worker` subagents with a two-sentence prompt: their agent name and "take a task".
4. Workers: `benchbook_next_task` -> `benchbook_brief` -> for each evaluation one Bash command,
   `benchbook --agent worker-<n> exp run --config '<json>' --seed <n> -- <command>`, which checks the ledger, runs
   the command and records the result -> claims with evidence -> `benchbook_complete_task`; final message <= 3
   sentences + ids. Without a shell they call `benchbook_check_tried` before and `benchbook_record_experiment`
   after each evaluation instead (the MCP tools stay). One command instead of three tool calls saves a turn per
   attempt; see [the SDK page](sdk.md#check-run-and-record-in-one-command) for its output and exit codes.
5. The lead reads `benchbook_status` and `benchbook_brief` (and `benchbook_sql`), decides claims, plans the next round.

You can decide as the human from a terminal: `benchbook decide c:1a2b3c4d verified --oracle human --rationale "..." --evidence e:...,e:... --as human`.

The stdio MCP server starts and lists tools before a project exists. If the lead runs `benchbook init` inside an
open session, its next ledger call opens the newly initialized project.

## The result-capture hook

When an `benchbook-worker` subagent stops, `benchbook hook subagent-stop` reads its final message. A report sent through
`SubagentHandback` is captured from the handback. A cited experiment
or claim ID counts when the subagent's transcript shows a successful write returning that ID (an MCP write, or
the receipt line of `benchbook exp record`, `exp run` or `claim add`) and the ledger
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

In a code-pack project, `[report] out_of_scope_heading` in `.benchbook/config.toml` makes the same processor record each
item of that section of the report as an out-of-scope finding claim (docs/code.md, "Out-of-scope findings").

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

## Declared checks: the Bash hook and the completion gate

In a project with `[checks]` in `.benchbook/config.toml` (the code pack, docs/code.md) two more hooks keep the
implementers' check runs in the ledger and hold a piece's task open until its HEAD passes. The plugin installs them;
the manual copy (`manual/settings.hooks.json`) has the same entries. Neither needs the agent to type a benchbook
command.

### `benchbook hook bash-result` (PostToolUse and PostToolUseFailure on Bash)

Claude Code fires `PostToolUse` only when a Bash command succeeds; a failure fires `PostToolUseFailure`, whose
`error` starts with `Exit code N`. The hook reads the command from `tool_input.command`, reads config.toml and
nothing else (no database module is imported), and compares the command with each declared check:

- Both are split into words the way the shell would, so quoting does not matter (`pytest -k 'a b'` is
  `pytest -k "a b"`).
- Leading `VAR=value` assignments (also after `env`) and redirections (`2>&1`, `> out.txt`) are dropped: they
  change the environment or where the output goes, not which check ran. A number written right before `>` is read
  as a file descriptor.
- One leading `cd DIR &&` is allowed; the check is then looked up, and its commit read, in DIR.
- The check must have run from the repository root, where a declared check runs; the same words run in a
  subdirectory are observed as `unrecordable`.
- A word with `{junit}` in the declaration matches a word of that shape or nothing. A raw Bash call for a
  `junit = true` check can record a known failure or timeout from the tool response. A successful raw call is
  observed as `unrecordable`: a shared report path cannot identify which invocation wrote the XML. Run
  `benchbook check run NAME` to record a pass. It supplies a separate temporary report path for each invocation
  and accepts a pass only when the report contains a test that ran and no failures. The completion gate ignores
  successful raw Bash records for JUnit checks, including ones recorded by older releases.
- The remaining words must be equal. Extra arguments (`-k upload`, `-x`) are a different command and do not
  match.

A match is one of three kinds, and every one is appended to `observed.jsonl` in the spool directory (the
denominator of the record coverage metric, docs/code.md):

| kind | when | recorded |
|---|---|---|
| `direct` | the command is the check | queued in the spool and recorded as an attempt with `via = "bash-hook"` |
| `wrapped` | `benchbook check run NAME` (also through `uv run` or `python -m benchbook`) | no: the wrapper records it itself, so it is counted once |
| `unrecordable` | the check inside a pipeline or list (`check \| tail`, `make && check`), run outside the repository root, in the background, interrupted, an error with no `Exit code` line, no piece or no commit | no: the exit code is not the check's, or there is none |

The attempt gets the rough signature: the exit code and a hash of the last line of the error text, normalised
(whitespace collapsed, digits zeroed). The output itself is never stored. A command that is not a declared check
leaves nothing behind (no spool entry, no log line) and the handler finishes within 50 ms
(`tests/test_hooks.py`). The hook always exits 0. Its timeout in hooks.json is 5 s.

### `benchbook hook task-completed` (TaskCompleted)

`TaskCompleted` fires when an agent marks a task completed with TaskUpdate, and when an agent-team teammate ends
its turn with tasks in progress. Exit code 2 keeps the task open and gives the hook's stderr to the model. The hook
works out the piece from the hook's `cwd` (`BENCHBOOK_PIECE`, else the branch name when `piece start` made that
branch a live piece) and asks the ledger whether
every check declared there has a passing attempt for that piece at HEAD: same `config.piece` and `code_ref`,
finished, not invalidated, from a clean checkout (`worktree_dirty` not true). Attempts from `check run`, from the
Bash hook and from `check verify` all count, and so does a passing run still waiting in the spool (check run
returns before its processor has written it). When one is missing it exits 2 with, for example:

```
benchbook: piece media has no passing run at HEAD 1a2b3c4d5e6f of: unit (not run), lint (its run failed).
Run this from /path/to/checkout, then mark the task completed again:
  benchbook check run unit --piece media && benchbook check run lint --piece media
```

(plus a line asking to commit first when the checkout has uncommitted paths). It fails open, exiting 0 with one
`task-completed: allowed: ...` line in `hook.log`, when:

| case | why open |
|---|---|
| no project for the cwd, or no `[checks]` | nothing is declared, so there is nothing to require |
| not in a git repository, HEAD detached with `BENCHBOOK_PIECE` unset, or no commit yet | there is no piece or no commit to look up |
| the branch is the base branch (`[guard] base`'s branch, `main` or `master`) and `BENCHBOOK_PIECE` is unset | the lead's own session and its task list are not a piece |
| the branch has no live piece (no `piece start <branch>`, or its lease ran out or it was released) and `BENCHBOOK_PIECE` is unset | the lead works on a feature branch too where nothing is committed to main; its task list is not a piece either |
| the project is not in the database, or does not use the code pack | its attempts could not be recorded anyway |
| benchbook raises anything (unreadable config.toml, the database unreachable within 5 s, a bug) | a broken benchbook must not hold every task open; the log line says `gate error` |

If the hook itself cannot start or runs past its 30 s timeout, Claude Code treats that as a non-blocking error, so
it is open too.

Every task completion in a piece's checkout is gated, the agent's own to-do items included; one passing run of
each check at HEAD opens all of them until the next commit. A branch is a piece's checkout only while its piece is
live: an implementer that works on a branch without `piece start` is not gated (set `BENCHBOOK_PIECE` to gate it).

### Under a token

Both hooks work the same with `BENCHBOOK_TOKEN`. The Bash hook only writes the spool; the processor it starts
records the attempt as the token's agent (no name is given, so it never claims to be someone else). Each token
has its own spool directory, and each entry carries a digest of the token it was queued with: a processor or
`benchbook hook replay` under another token (or none) leaves it queued, so agents that share a database URL and
a state directory never record each other's entries. Replay an entry with the token it was queued under. After
a rotation (`benchbook agent add NAME --rotate`) the old token no longer authenticates, so the processor notes in
each token's spool (`writer.info`) which agent the ledger said that token was; `benchbook hook replay` under the
new token moves into its own spool the entries of the old one whose project names the same agent there, and records
them. Each entry keeps its original receipt through repeated rotations. For a check attempt, that receipt is its
experiment ID in the ledger, so a retry after the database commits reuses the same row even if local cleanup or
writing `adopted.done` failed. Separate check executions keep separate receipts. Every token's spool notes its
database target when its first entry is queued, so entries of a token no processor ever confirmed (the ledger was
unreachable for its whole life) are found too: they stay where they are, and replay counts them as `held`, names
the folder and exits non-zero. If replay cannot check the current token's agent because the database is
unavailable, it reports the entries still queued. A check that names an agent (`--agent` or `BENCHBOOK_AGENT`)
different from the token's agent moves to `dead/`; run the check with that agent's token to record its result. On a
`benchbook_writer` login without a token the processor cannot register `hook` and the entry goes to `dead/`, as
for the result-capture hook. The gate only reads (`projects`, `experiments_valid`), which the writer login may.

### Can SubagentStop block a subagent started with the Agent tool? (open question 1)

The hooks reference (cc_hooks.md, 2026-11-21) says yes. Its exit-code table lists:

> `SubagentStop` | Yes | Prevents the subagent from stopping

and the SubagentStop section says:

> SubagentStop hooks use the same decision control format as Stop hooks [...] Returning `decision: "block"` with a
> `reason` keeps the subagent running and delivers `reason` to the subagent as its next instruction. A hook that
> blocks by exiting 2 delivers its stderr message the same way.

SubagentStart, whose matcher values SubagentStop shares, "Runs when Claude spawns a subagent with the Agent tool".
The same page warns that "Not every SubagentStop event comes from a subagent Claude spawned": internal agents
(prompt suggestions, `/btw`) fire it too, so a gate there would have to check `agent_type`.

**Not measured.** This answer is from the documentation only; confirming it needs a model run, which was not
spent. The plugin does not gate on SubagentStop: the completion gate is the TaskCompleted hook above.

## The boundary hooks

The same startup brief and pending notes are available on Codex, Cursor, Gemini CLI, OpenCode V2 and Pi through
their own hook or extension APIs. [Harness setup and event support](harnesses.md)

When there are dead or overdue spool entries or recent hook failures, the code brief opens with a recording
warning and the replay command. `benchbook statusline` reads a one-line summary of verified pieces and open
findings from the processor's cache. Output is empty until the processor records its first entry for this project.

The `benchbook` plugin also gives the model what the ledger says at the points where a session loses it
(`src/benchbook/boundary_hooks.py`):

| hook | event | what it prints |
|---|---|---|
| `benchbook hook session-start` | SessionStart (every source: startup, resume, clear, compact) | the brief scoped by what this branch changed (`brief --here`), at most 1,500 tokens and 6,000 characters, as `additionalContext`; nothing when every section is empty |
| `benchbook hook pre-compact` | PreCompact | one paragraph of at most 1,000 characters, "Keep these ids and statuses verbatim in the summary: ...", three lines per section, on plain stdout (instructions for the compaction summary) |
| `benchbook hook notes` | UserPromptSubmit | notes queued for this session in `<state dir>/notes/<session_id>/` (the automatic re-run, T11), once; they move to `delivered/` |
| `benchbook hook agent-result` | PostToolUse (Agent or Task), SubagentStop | queues a re-run when a report identifies its commit and piece. On PostToolUse (a foreground report) it says so and delivers any results already waiting for this session; on SubagentStop (an agent Claude Code ran in the background, which is every Agent call on 2.1.330) it prints nothing, and the "re-running" line waits as a note for the task notification's UserPromptSubmit. A SubagentStop with an empty `agent_type` is the compaction summarizer and is skipped |

The detached processor verifies the reported commit using the project's declared checks. It stores the report's
hash and structured identifiers. Repeated reports for the same project, piece and commit share one completed
re-run within the database and writer's spool. An environment failure is kept for that report in its session, so a
new report or session can re-run the checks after the environment recovers. SessionStart, UserPromptSubmit, and
subsequent Agent or Bash results can deliver the pending note once. For a background agent, SubagentStop queues
the check after the report arrives.

Each brief is also written to `.benchbook/STATE.md` for harnesses without hooks and for people. Its first line
names the time, HEAD, project and connection fingerprint. When the benchbook folder is tracked, STATE.md is added
to `.git/info/exclude`. The ledger has 5 seconds to answer, including connection, queries and the brief's git calls.
SessionStart can supply the last STATE.md when the embedded ledger times out or a connection fails at the transport
level. Fallback requires the same project and a matching database fingerprint. For an external PostgreSQL server,
the fingerprint covers the effective libpq endpoint, database and login without including credentials. A service
name, an implicit socket directory or multiple endpoints can make the target ambiguous. In those cases, with a
Benchbook token, or after a remote timeout or authorization denial, the hook leaves the snapshot for manual use.
PreCompact only gives live context. Outside a benchbook project each hook leaves after reading config.toml, without
importing the database layer. The hooks log failures in `hook.log` and exit 0.

## Boundary hooks: what Claude Code does

The boundary benchmark exercises its report pipeline with scripted inputs using
`uv run python -m bench.boundary.run --dry`. Collecting evidence from real Claude Code sessions requires the
conditions in the [benchmark preregistration](../bench/boundary/PREREGISTRATION.md).

The session-start, compaction and Agent-result hooks rely on the inputs and outputs below. The "documented" column is
from the hook reference (code.claude.com/docs/en/hooks, read 2026-11-22); `scripts/qa_boundary_hooks.sh`
(MANUAL_QA.md, scenario 5) checks them on a real session and fills the last column (2026-11-22, Claude Code
2.1.330, haiku). The fixtures in `tests/fixtures/hooks/` are the inputs it saw, with session ids, paths and report
text replaced (`session_start_startup.json`, `session_start_compact.json`, `pre_compact.json`,
`post_tool_use_agent_async.json`, `user_prompt_submit.json`, `user_prompt_submit_task_notification.json`,
`subagent_stop_background.json`, `subagent_stop_compaction.json`), except `post_tool_use_agent.json` (a completed
foreground result), which no default session produced and which stays the documented example.

| question | documented | observed (Claude Code version) |
|---|---|---|
| SessionStart `source` values | `startup`, `resume`, `clear`, `compact`, `fork` | `startup`, `resume` (each `--resume`), `compact` seen; `model` present on `compact`, absent on `startup` (2.1.330) |
| SessionStart `additionalContext` reaches the model, also after compaction | yes; `compact` is the documented way to re-inject | yes, before and after `/compact` (2.1.330) |
| PreCompact input | `trigger` (`manual`/`auto`), `custom_instructions` (may be null) | as documented, plus `prompt_id`; `trigger` `manual` for `/compact` (2.1.330) |
| PreCompact stdout | instructions for the compaction summary, cut at 10,000 characters | followed: the phrase it asked for is in PostCompact's `compact_summary` (2.1.330). Compaction also ends with a SubagentStop whose `agent_type` is `""` and whose `last_assistant_message` is the summary |
| PostToolUse on the Agent tool | `tool_name` `Agent` (no `Task` alias); `tool_response.status` `completed` with `content`, or `async_launched` with `outputFile` | `Agent`; **both calls `async_launched`**, the one without `run_in_background` too (`isAsync: true`). The report came with SubagentStop (`agent_type`, `last_assistant_message`), then a UserPromptSubmit whose `prompt` is a `<task-notification>` holding it in `<result>` (2.1.330) |
| UserPromptSubmit when a background subagent reports | fires | fires once per finished agent, right after its SubagentStop (2.1.330) |
| PostToolUse (Bash) `additionalContext` reaches the model | yes | yes (2.1.330) |
| Both plugins load from repeated `--plugin-dir` | not documented | yes: `benchbook` and `benchbook-mcp` (2.1.330) |
| MCP tool prefix of the `benchbook-mcp` plugin | `mcp__plugin_<plugin>_<server>__`; a hyphen in the plugin name not addressed | `mcp__plugin_benchbook-mcp_benchbook__`: the hyphen stays. The server is still `pending` at `init` and connects during the first turn (2.1.330) |

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

See `integrations/agents-md/AGENTS.md`: the same rules, runtime-neutral, and an MCP config example. The Bash hook
and the result-capture hook also come as Codex and Cursor manifests (`integrations/codex/`, `integrations/cursor/`;
docs/harnesses.md says what each harness's hook input lets them capture).
