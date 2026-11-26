# Claude Code

## What happens in a session

| when | what benchbook does |
|---|---|
| a session starts, resumes or is compacted | gives the brief for what this branch changed (at most 1,500 tokens): verified pieces and how they were checked, re-runs nobody decided and why, "reported done, not re-run", discarded approaches, open findings, occupied paths. It is also written to `.benchbook/STATE.md` |
| right before compaction | asks the summary to keep the record ids and statuses verbatim |
| a subagent reports `DONE <sha>` | re-runs the declared checks on that commit in a clean worktree, in the background, as `rerun@<host>` (an orchestrator, so a pass decides `verified`) |
| the next prompt, Bash call or agent result | delivers the outcome once: `benchbook: media-upload a1b2c3d: re-run passed (2 checks, 14.2s), tests not weakened, checked: re-run (c:7f3a91c2)` |
| the lead runs `git merge` on a reported commit | waits up to 30 s for that commit's re-run and denies the merge once unless it passed cleanly (the merge gate) |
| an agent runs a declared check in Bash | records the run against the piece and the commit |
| an agent marks a task completed on a piece's branch (`piece start`) | keeps it open until every declared check has a passing run at HEAD, and says which command to run |
| recording breaks (a dead report, hook failures) | starts the brief with `benchbook: recording stopped: ...` and the fix |

The brief at session start and after compaction is experimental: in four pilots a lead that read it did not know
more than one that kept progress files ([results](results.md)). The re-run of a reported commit and the
weakened-test guard do not depend on it.

Hooks never write to stderr and always exit 0, except the completion gate, which exits 2 with a message on stderr to
keep a task open. Outside a benchbook project they leave without touching the database.

## Install

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook
benchbook install --no-cli          # the marketplace and the hook plugin; --mcp adds benchbook-mcp
```

`benchbook install [--harness claude-code|codex|cursor|gemini] [--mcp] [--no-cli] [--dry-run]` installs the CLI at its
own version (`uv tool install benchbook==<version>` for a release; a development build from the git repository and
commit it was installed from, and left as it is when that source is unknown, such as a local checkout), adds the `Jake-Network/benchbook` marketplace and installs the hook plugin `benchbook@benchbook`.
`--mcp` adds `benchbook-mcp@benchbook` and `--dry-run` prints the steps. Without a `claude` command it prints the
plugin steps for you to run. For `codex`, `cursor` and `gemini` it merges that harness's packaged manifest into the
user-level file instead ([harnesses](harnesses.md)).

`benchbook uninstall` removes the plugins (also the pre-rename `assay@assay`; without a `claude` command it prints the
step), benchbook's entries in the Codex, Cursor and Gemini CLI manifests, the state directory (spool, logs, notes),
this checkout's `.benchbook/` when `git ls-files` answers that it tracks nothing there (a failing git or a corrupt
index keeps the folder) and its `.git/info/exclude` line. `--purge` also removes the embedded database when its
directory contains the PostgreSQL version, data and control files and `pg_ctl status` confirms that its server has
stopped.

Claude Code loads a plugin's MCP server whenever the plugin is enabled, and every tool definition is in every
prompt. That is why the hooks and the MCP server are two plugins: the hook plugin costs no tokens per turn.

### PATH and the data directory

Claude Code starts `benchbook mcp` and `benchbook hook` from its own PATH. If `command -v benchbook` finds nothing in
a new terminal, run `uv tool update-shell` once (it adds `~/.local/bin` to your shell profile), open a new terminal
and restart Claude Code. Without it the MCP server does not connect, and every hook is skipped (exit 0) with a line
in `missing-cli`, which `benchbook doctor` reports as `hook cli`.

On macOS the embedded database lives in `~/Library/Application Support/benchbook/pg`. To keep it elsewhere set
`BENCHBOOK_DATA_DIR`, and set it in Claude Code's `settings.json` `env` too: the CLI, MCP server and hook must see the
same value.

### Updating

Claude Code keeps an installed plugin version until you update it. After pulling a new benchbook, update the plugin as
well and restart Claude Code:

```bash
claude plugin marketplace update benchbook && claude plugin update benchbook@benchbook
```

Bump `version` in `integrations/claude-code/.claude-plugin/plugin.json` whenever a file under
`integrations/claude-code/` changes; an install of the same version does not pick the change up.

### From a checkout

```bash
uv tool install --from /path/to/benchbook benchbook   # puts `benchbook` on PATH
claude plugin marketplace add /path/to/benchbook      # the repository root has .claude-plugin/marketplace.json
claude plugin install benchbook@benchbook             # hooks only: no MCP tools in the session
claude plugin install benchbook-mcp@benchbook         # optional: the MCP server, worker subagent and lead skill
```

For development, `claude --plugin-dir integrations/claude-code --plugin-dir integrations/claude-code-mcp` loads both
without installing.

### Install by hand (no plugin)

```bash
cp integrations/claude-code/manual/.mcp.json .mcp.json
mkdir -p .claude/agents .claude/skills
cp integrations/claude-code/manual/agents/benchbook-worker.md .claude/agents/
cp -r integrations/claude-code-mcp/skills/benchbook integrations/claude-code-mcp/skills/benchbook-lead .claude/skills/
```

Merge `integrations/claude-code/manual/settings.hooks.json` into `.claude/settings.json`. The skill is then
`/benchbook-lead`. Tool names differ between the two installs (`mcp__plugin_benchbook-mcp_benchbook__*` vs
`mcp__benchbook__*`), which is why the manual worker file is separate.

## Doctor

Run `benchbook doctor` in the project directory. It prints the build (`benchbook build`, as `benchbook --version`),
`python` (3.11 or newer), the database lines (`connect`, `postgres >= 15`, `pgvector (optional)`,
`benchbook_reader role`, `db schema` next to `build schema head`, the newest revision this build knows) and `project
config`, then what Claude Code needs. Each failing check prints the command that fixes it:

| check | fails when |
|---|---|
| `benchbook on PATH` | `benchbook` is not on the PATH Claude Code inherits (or the `PATH` in its settings `env`) |
| `claude code plugin` | `benchbook@benchbook` is not installed or not enabled (a manual install with `.mcp.json` passes) |
| `plugin hooks` | the installed plugin is older than the hooks this build needs; the line names the command that updates it |
| `hook cli` | the plugin's script found no `benchbook` on PATH and skipped hooks (it notes each in `missing-cli`) |
| `BENCHBOOK_DATABASE_URL`, `BENCHBOOK_DATA_DIR`, `BENCHBOOK_PROJECT` | the shell value selects a different database or project than the `env` in `~/.claude/settings.json`, `.claude/settings.json` or `.claude/settings.local.json` (the URL itself is never printed) |
| `hook failures` | for this shell's database: `hook.log` has failure lines from the last 24 hours (after the last clean `benchbook hook replay` of its spool), or its spool holds reports in `dead/` or queued for over 5 minutes |
| `harness hook input` | another harness's hook sent input benchbook cannot read ([harnesses](harnesses.md#doctor)) |

Without `~/.claude` the Claude Code checks are skipped. Last come the lines for an install from before the rename
(`config folder`, `data directory`, `state directory`, `legacy variables`): they pass and name the command that moves
each ([below](#installs-from-before-the-rename-assay)).

## The plugins

The hook plugin (`integrations/claude-code/`, `benchbook@benchbook`) runs every hook through `bin/benchbook-hook`: it
runs `benchbook hook EVENT` from PATH (`assay hook` when only that is there); without either it notes one line in
`${CLAUDE_PLUGIN_DATA}/missing-cli` and exits 0. Every command in `hooks/hooks.json` is
`"${CLAUDE_PLUGIN_ROOT}/bin/benchbook-hook" EVENT`, so the plugin calls only a file of its own (the directory's
submission checklist asks for that).

| event (matcher) | `benchbook hook ...` | timeout |
|---|---|---|
| SessionStart | `session-start` | 10 s |
| PreCompact | `pre-compact` | 10 s |
| UserPromptSubmit | `notes` | 5 s |
| PreToolUse (Bash, `if: "Bash(git merge *)"`) | `bash-start`, the merge gate | 45 s |
| PostToolUse and PostToolUseFailure (Bash) | `bash-result` | 5 s |
| PostToolUse (`Agent\|Task`) | `agent-result` | 5 s |
| PostToolUse (`SubagentHandback`) | `subagent-handback` | 5 s |
| PostToolUse (`^(?!(Bash\|Agent\|Task)$)`) | `notes` | 5 s |
| SubagentStop (every subagent) | `subagent-stop`, then `agent-result` | 5 s each |
| TaskCompleted | `task-completed`, the completion gate | 30 s |

The MCP plugin (`integrations/claude-code-mcp/`, `benchbook-mcp@benchbook`) contains:

| file | what |
|---|---|
| `.mcp.json` | the `benchbook` MCP server (`benchbook mcp`, stdio) |
| `agents/benchbook-worker.md` | worker subagent: worker tools only (no `benchbook_decide`, `benchbook_create_tasks`, `benchbook_cancel_task`) |
| `skills/benchbook/SKILL.md` | rules for any agent using the ledger |
| `skills/benchbook-lead/SKILL.md` | the lead loop, `/benchbook-mcp:benchbook-lead <goal>` |

## The loop

1. `benchbook init` (or the lead does it): goal, metric, direction. `lead` is the orchestrator, `human` is you.
2. The lead creates 4-8 tasks with `spec.config_space` and tags.
3. It launches K `benchbook-worker` subagents with a two-sentence prompt: their agent name and "take a task".
4. Workers: `benchbook_next_task` -> `benchbook_brief` -> for each evaluation one Bash command,
   `benchbook --agent worker-<n> exp run --config '<json>' --seed <n> -- <command>`, which checks the ledger, runs
   the command and records the result -> claims with evidence -> `benchbook_complete_task`; final message a short
   summary + ids. Without a shell they call `benchbook_check_tried` before and `benchbook_record_experiment`
   after each evaluation instead (the MCP tools stay). One command instead of three tool calls saves a turn per
   attempt; see [the SDK page](sdk.md#check-run-and-record-in-one-command) for its output and exit codes.
5. The lead reads `benchbook_status` and `benchbook_brief` (and `benchbook_sql`), decides claims, plans the next round.

You can decide as the human from a terminal: `benchbook decide c:1a2b3c4d verified --oracle human --rationale "..." --evidence e:...,e:... --as human`.

The stdio MCP server starts and lists tools before a project exists. If the lead runs `benchbook init` inside an
open session, its next ledger call opens the newly initialized project.

## The result-capture hook

When a `benchbook-worker` subagent stops, `benchbook hook subagent-stop` reads its final message. A report sent
through `SubagentHandback` is captured from the handback. A cited experiment or claim ID counts when the subagent's
transcript shows a successful write returning that ID (an MCP write, or the receipt line of `benchbook exp record`,
`exp run` or `claim add`) and the ledger timestamps place the record within the run. The hook captures the report
when the transcript is missing or the citation cannot establish that the run wrote the record. A `note` claim tagged
`auto-captured` keeps the first 300 characters as its statement and the first 4,000 in `payload.text`. Identical
message bodies merge across hook events. Reports with a shared opening and different conclusions each get a record.

The hook fires for every subagent, and `BENCHBOOK_HOOK_AGENTS` (comma separated, default `benchbook-worker`, with or
without a plugin prefix) decides which are captured (`assay-worker`, the worker's name before the rename, counts as
`benchbook-worker`); `BENCHBOOK_HOOK_AGENTS=*` captures every subagent. To capture your own worker types too, set
`BENCHBOOK_HOOK_AGENTS=benchbook-worker,implementer` in your shell and in the `env` of Claude Code's `settings.json`.
Other subagents (Explore, general-purpose, ...) are not captured, unless `BENCHBOOK_HOOK_AGENTS` is unset and
`[report] agents` lists them: then only their out-of-scope findings are recorded (docs/code.md, "Which reports are
read"). An event without an agent type is not captured, and neither is the compaction summarizer (a SubagentStop
with an empty `agent_type`), even with `*`. This works the same for the plugin and the manual install.

In a code-pack project, `[report] out_of_scope_heading` in `.benchbook/config.toml` makes the same processor record
each item of that section of the report as an out-of-scope finding claim (docs/code.md, "Out-of-scope findings").

The hook uses the project selected by `.benchbook/config.toml` or `BENCHBOOK_PROJECT`; when it finds neither (for
example a worktree without a committed `.benchbook/`) it writes a "no benchbook project" line to the log. It writes
the report to a local spool, keyed by database target, and exits 0 while a detached process handles the database
write; only one processor drains a spool at a time. The processor retries connection failures for about a minute and
keeps those reports for the next hook. Failures a retry cannot fix (the project does not exist in this database, the
schema is newer than this benchbook) move the report to the spool's `dead/` folder. Malformed spool files are
quarantined with the `.invalid` suffix. An entry without a recorded capture decision whose agent type the current
`BENCHBOOK_HOOK_AGENTS` does not cover also goes to `dead/`; `BENCHBOOK_HOOK_AGENTS=<the original list> benchbook hook
replay` captures it. After an outage or a fix, `benchbook hook replay` retries pending and dead reports once in the
foreground using the same database settings. See `~/.local/state/benchbook/hook.log` for capture and retry status.

When there are dead or overdue spool entries or recent hook failures, the code brief opens with a recording warning
and the replay command (docs/code.md, "The brief").

## Declared checks: the Bash hook and the completion gate

In a project with `[checks]` in `.benchbook/config.toml` (the code pack, docs/code.md) these hooks keep the
implementers' check runs in the ledger, hold the lead's merge of a reported commit until its re-run, and hold a
piece's task open until its HEAD passes. The plugin installs them; the manual copy (`manual/settings.hooks.json`) has
the same entries. None needs the agent to type a benchbook command.

### `benchbook hook bash-start` (PreToolUse on Bash, `git merge` only): the merge gate

The manifest runs it only for `Bash(git merge *)` (the hook's `if`, so other Bash calls pay no start-up). When the
lead (not a subagent) merges a ref whose commit a report queued a re-run for in this session, it waits up to 30 s
for that re-run's verdict note. A clean verdict (`re-run passed`, `tests not weakened`) lets the merge run and is
given to the lead as the call's `additionalContext`; any other verdict, or none within 30 s, denies the call once
with the verdict or the wait as the reason. Merging the same commit again goes through. The pending marks are
`pending/<sha>.json` in the session's notes directory (`verify_requests.enqueue` writes them, with the reporting
agent's type).

The reason ends with what the lead can run, by verdict:

| verdict | the hold message |
|---|---|
| none within 30 s | wait for the line (a failed run is retried, which takes longer); if none comes, `benchbook --agent lead check verify <piece> <sha> --reported-by <reporter>`, which re-runs and decides |
| `re-run failed (...), then passed on a retry` (docs/code.md) | if the failure is not the piece's, `benchbook --agent lead decide e:<failed run> invalidated --oracle human --rationale "..."` and then that `check verify`; otherwise a person decides |
| `re-run failed, and again on a retry with a different failure` | if neither failure is the piece's, invalidate both runs and run that `check verify`; otherwise the implementer fixes it, or a person decides |
| `re-run failed, and again on a retry` | the implementer fixes it and reports a new commit, or a person decides |
| `re-run failed` (no retry: the retry could not run, or a verdict from before it) | the implementer fixes it; if the failure is not the piece's, invalidate the failed run and run that `check verify`; or a person decides |
| `tests weakened ... needs review` | a person decides (`benchbook decide <claim> verified\|refuted --oracle human --as human --evidence <attempt ids> --rationale "..."`) |

### `benchbook hook bash-result` (PostToolUse and PostToolUseFailure on Bash)

Claude Code fires `PostToolUse` only when a Bash command succeeds; a failure fires `PostToolUseFailure`, whose
`error` starts with `Exit code N`. For the lead the hook first delivers the session's pending notes, as the notes hook
does. Then it reads the command from `tool_input.command`, reads config.toml and nothing else (no database module is
imported), and compares the command with each declared check:

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
  successful raw Bash records for JUnit checks.
- The remaining words must be equal. Extra arguments (`-k upload`, `-x`) are a different command and do not
  match.

A `direct` match (the command is the check) is queued in the spool and recorded as an attempt with
`via = "bash-hook"`. `benchbook check run NAME` (also through `uv run` or `python -m benchbook`) is `wrapped`: the
wrapper records it itself. A check inside a pipeline or list (`check | tail`, `make && check`), run outside the
repository root or in the background, interrupted, or without an exit code, piece or commit is `unrecordable`:
observed, not recorded. Every match is appended to `observed.jsonl` in the
spool directory (docs/code.md, "Record coverage").

A failed attempt gets a failure signature from the tool result's error text (docs/code.md, "Failure signatures"); the
output itself is never stored. A command that is not a declared check leaves nothing behind (no spool entry, no log
line) and the handler finishes within 50 ms (`tests/test_hooks.py`). The hook always exits 0.

### `benchbook hook task-completed` (TaskCompleted)

`TaskCompleted` fires when an agent marks a task completed with TaskUpdate, and when an agent-team teammate ends
its turn with tasks in progress. Exit code 2 keeps the task open and gives the hook's stderr to the model. The hook
works out the piece from the hook's `cwd` (`BENCHBOOK_PIECE`, else the branch name when `piece start` made that
branch a live piece) and asks the ledger whether every check declared there has a passing attempt for that piece at
HEAD: same `config.piece` and `code_ref`, finished, not invalidated, from a clean checkout (`worktree_dirty` not
true). Attempts from `check run`, from the Bash hook and from `check verify` all count, and so does a passing run
still waiting in the spool (check run returns before its processor has written it). When one is missing it exits 2
with, for example:

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

The gate is on TaskCompleted, not SubagentStop: the plugin does not keep a subagent running from its stop hook
(DECISIONS.md).

### Under a token

With `BENCHBOOK_TOKEN` the hooks work the same, and the processor records as the token's agent. Each token has its
own spool, and an entry is recorded only under the token it was queued with: replay it with that token. After
`benchbook agent add NAME --rotate`, `benchbook hook replay` under the new token adopts the old token's entries.
Replay counts entries it cannot record under the current token as `held`, names their folder and exits non-zero. A
check that names an agent (`--agent` or `BENCHBOOK_AGENT`) other than the token's, and any entry queued on a
`benchbook_writer` login without a token, go to `dead/`. The gate only reads, which the writer login may. Tokens and
the writer login: [docs/multi-machine.md](multi-machine.md#per-agent-tokens).

## The boundary hooks

The same startup brief and pending notes are available on Codex, Cursor, Gemini CLI, OpenCode V2 and Pi through
their own hook or extension APIs. [Harness setup and event support](harnesses.md)

`benchbook statusline` reads a one-line summary of verified pieces and open findings from the processor's cache.
Output is empty until the processor records its first entry for this project.

The `benchbook` plugin also gives the model what the ledger says at the points where a session loses it
(`src/benchbook/hooks/boundary.py`):

| hook | event | what it prints |
|---|---|---|
| `benchbook hook session-start` | SessionStart (every source: startup, resume, clear, compact) | the brief scoped by what this branch changed (`brief --here`), at most 1,500 tokens and 6,000 characters, as `additionalContext`; nothing when every section is empty |
| `benchbook hook pre-compact` | PreCompact | one paragraph of at most 1,000 characters, "Keep these ids and statuses verbatim in the summary: ...", three lines per section, on plain stdout (instructions for the compaction summary) |
| `benchbook hook notes` | UserPromptSubmit; PostToolUse of every tool but Bash, Agent and Task (those deliver from their own hooks) | notes queued for this session in `<state dir>/notes/<session_id>/` (the outcome of an automatic re-run), once, under the event that ran; they move to `delivered/`. A hook that fired inside a subagent (`agent_id` in its input) delivers nothing, so the notes wait for the lead |
| `benchbook hook agent-result` | PostToolUse (Agent or Task), SubagentStop | queues a re-run when a report identifies its commit and piece. On PostToolUse (a foreground report) it says so and delivers any results already waiting for this session; an `async_launched` result queues nothing and delivers what is waiting. On SubagentStop (a background agent) it prints nothing, and the "re-running" line waits as a note for the task notification's UserPromptSubmit. When a report comes as both SubagentStop and PostToolUse, the PostToolUse answer takes the waiting line, so the lead reads it once, and the re-run is not queued again. A SubagentStop with an empty `agent_type` is the compaction summarizer and is skipped |

- **Re-runs.** The detached processor verifies the reported commit using the project's declared checks. Re-runs have
  a lane of their own (the spool's verify lane, `.verify.lock`), so reports and check-run attempts queued meanwhile
  are recorded without waiting for them; each re-run first records what was queued before it. It stores the report's
  hash and structured identifiers. Repeated reports for the same project, piece and commit share one completed
  re-run within the database and writer's spool. An environment failure is kept for that report in its session, so
  a new report or session can re-run the checks after the environment recovers.
- **Delivery.** SessionStart, UserPromptSubmit and the lead's later tool results (any tool) deliver a pending note
  once. Only the lead's: Claude Code gives a subagent's hooks the lead's `session_id` with the subagent's
  `agent_id`, and a worker's tool result that took a verdict would leave the lead without it.
- **STATE.md.** Each brief is also written to `.benchbook/STATE.md` for harnesses without hooks and for people. Its
  first line names the time, HEAD, project and connection fingerprint. When the benchbook folder is tracked,
  STATE.md is added to `.git/info/exclude`.
- **Time limit and fallback.** The ledger has 5 seconds to answer, including connection, queries and the brief's git
  calls. SessionStart can supply the last STATE.md when the embedded ledger times out or a connection fails at the
  transport level, for the same project and a matching database fingerprint. For an external PostgreSQL server, the
  fingerprint covers the effective libpq endpoint, database and login without including credentials. A service
  name, an implicit socket directory or multiple endpoints can make the target ambiguous. In those cases, with a
  benchbook token, or after a remote timeout or authorization denial, the hook leaves the snapshot for manual use.
  PreCompact only gives live context.
- Outside a benchbook project each hook leaves after reading config.toml, without importing the database layer. The
  hooks log failures in `hook.log` and exit 0.

## Boundary hooks: what Claude Code does

The boundary benchmark exercises its report pipeline with scripted inputs using
`uv run python -m bench.boundary.run --dry`. Collecting evidence from real Claude Code sessions requires the
conditions in the [benchmark preregistration](../bench/boundary/PREREGISTRATION.md).

The session-start, compaction and Agent-result hooks rely on the inputs and outputs below. The "documented" column is
from the hook reference (code.claude.com/docs/en/hooks, read 2026-11-22); `scripts/qa_boundary_hooks.sh`
(MANUAL_QA.md, scenario 5) checks them on a real session and fills the last column, with the Claude Code version it
ran. The fixtures in `tests/fixtures/hooks/` are the inputs it saw, with session ids, paths and report text replaced
(`session_start_startup.json`, `session_start_compact.json`, `pre_compact.json`, `post_tool_use_agent_async.json`,
`user_prompt_submit.json`, `user_prompt_submit_task_notification.json`, `subagent_stop_background.json`,
`subagent_stop_compaction.json`), except `post_tool_use_agent.json` (a completed foreground result), which stays the
documented example.

| question | documented | observed (Claude Code version) |
|---|---|---|
| SessionStart `source` values | `startup`, `resume`, `clear`, `compact`, `fork` | `startup`, `resume` (each `--resume`), `compact` seen; `model` present on `compact`, absent on `startup` (2.1.330) |
| SessionStart `additionalContext` reaches the model, also after compaction | yes; `compact` is the documented way to re-inject | yes, before and after `/compact` (2.1.330) |
| PreCompact input | `trigger` (`manual`/`auto`), `custom_instructions` (may be null) | as documented, plus `prompt_id`; `trigger` `manual` for `/compact` (2.1.330) |
| PreCompact stdout | instructions for the compaction summary, cut at 10,000 characters | followed: the phrase it asked for is in PostCompact's `compact_summary` (2.1.330). Compaction also ends with a SubagentStop whose `agent_type` is `""` and whose `last_assistant_message` is the summary |
| PostToolUse on the Agent tool | `tool_name` `Agent` (no `Task` alias); `tool_response.status` `completed` with `content`, or `async_launched` with `outputFile` | `Agent`. Both calls `async_launched`, the one without `run_in_background` too (`isAsync: true`); the report came with SubagentStop (`agent_type`, `last_assistant_message`), then a UserPromptSubmit whose `prompt` is a `<task-notification>` holding it in `<result>` (2.1.330). A foreground report came as SubagentStop, then PostToolUse (2.1.331). The call without `run_in_background` is `completed` in some runs and `async_launched` in others (2.1.332) |
| UserPromptSubmit when a background subagent reports | fires | fires once per finished agent, right after its SubagentStop (2.1.330) |
| PostToolUse (Bash) `additionalContext` reaches the model | yes | yes (2.1.330) |
| Both plugins load from repeated `--plugin-dir` | not documented | yes: `benchbook` and `benchbook-mcp` (2.1.330) |
| TaskCompleted on a feature branch | the hook decides | `claude -p` offers `TaskCreate`/`TaskUpdate`. With no `piece start` for the branch the update completed (`allowed: feature-plan has no live piece`); after `piece start feature-plan` it was held and the model quoted the hook's message (2.1.330) |
| A subagent's `DONE <sha>` | not documented | the line was `checked: re-run` and the claim `verified` (oracle `rerun`), decided by `rerun@<host>`; when both the Agent result and SubagentStop carried the report, both queued the re-run, which ran and was delivered once (2.1.330) |
| MCP tool prefix of the `benchbook-mcp` plugin | `mcp__plugin_<plugin>_<server>__`; a hyphen in the plugin name not addressed | `mcp__plugin_benchbook-mcp_benchbook__`: the hyphen stays. The server is still `pending` at `init` and connects during the first turn (2.1.330) |
| A subagent's hook inputs (scenario 6) | not documented | its Bash PostToolUse carries the lead's `session_id` and an `agent_id`; a lead Read result's `additionalContext` through the notes hook's matcher reaches the model (2.1.332) |
| PreToolUse with `if: "Bash(git merge *)"` (the merge gate, scenario 7) | `if` filters by permission-rule syntax; a deny's `permissionDecisionReason` is shown to Claude; `additionalContext` is added next to the tool result | ran for the two `git merge` calls and not for `git status`; the deny reason reached the model (as `PreToolUse:Bash hook error: <reason>`) and so did the allow's `additionalContext` (2.1.332) |

## Installs from before the rename (assay)

An install from before the rename to benchbook keeps working in benchbook 0.1.x (`benchbook --version`) with nothing
changed: `ASSAY_*` variables, `.assay/`, the old data and state directories, the `assay` command and the `assay@assay`
plugin are still read, and nothing is moved for you. `benchbook doctor` lists what it found under the old name. To
move to the new names, in any order and whenever you like:

```bash
git mv .assay .benchbook                         # in each repository (plain mv when the folder is not committed)
benchbook db stop && mkdir -p ~/.local/share/benchbook && mv ~/.local/share/assay/pg ~/.local/share/benchbook/pg
mv ~/.local/state/assay ~/.local/state/benchbook   # with no Claude Code session running
claude plugin marketplace add Jake-Network/benchbook && claude plugin install benchbook@benchbook   # then remove assay@assay
```

On macOS the data directory is `~/Library/Application Support/assay/pg`; `benchbook doctor` prints the exact command.
Rename `ASSAY_*` variables to `BENCHBOOK_*` in your shell profile and in Claude Code's `settings.json` together: the
CLI, the MCP server and the hook must still see the same database.

## Other MCP clients (Codex, ...)

See `integrations/agents-md/AGENTS.md`: the same rules, runtime-neutral, and an MCP config example. The hooks also
come as manifests for Codex, Cursor and Gemini CLI and as extensions for OpenCode and Pi ([harnesses](harnesses.md)).
