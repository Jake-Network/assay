# Decisions

One line per decision the spec left open or that changed during implementation. Pn = product principle.

## M0 spike (2026-11-19)

- **Embedded Postgres:** `pixeltable-pgserver` 0.7.2, PG 16 pinned (`postgres_version=16`; the wheel also ships PG 18).
  Verified on Linux x86_64 / WSL2 with Python 3.13; macOS and Python 3.11/3.12 are covered by the CI matrix,
  not verified by hand. pgvector is included, pgcrypto is not (not needed: `gen_random_uuid()` is core). (P7)
- **Server lifetime:** `cleanup_mode=None`: the first process starts the server and nobody stops it on exit,
  so one Claude Code session ending never cuts off another (`cleanup_mode='stop'` would). `assay db stop`
  stops it explicitly. Verified: two processes connected at once, the second keeps working after the first exits.
- **Start-up race:** the package already serialises start-up with an inter-process file lock (`fasteners`),
  so assay does not add its own `fcntl.flock`. All embedded-server code stays in `db/conn.py`.
- **MCP SDK:** `mcp` 2.x is current; `FastMCP` is now `mcp.server.mcpserver.MCPServer`. Pinned `mcp>=2.3,<3`.
  Expected failures are raised as `ToolError` so the model sees the message (any other exception is masked).
- **Hook input (code.claude.com/docs, 2026-11-19):** `SubagentStop` carries `last_assistant_message` (the
  subagent's final text). When a subagent reports through `SubagentHandback`, the report is in
  `tool_input.message` of a `PostToolUse` event for that tool. The plugin hooks both; the handler also reads
  `PostToolUse` on `Agent` (`tool_response` text blocks) if wired manually.
- **Plugin format:** `.claude-plugin/plugin.json`, `.mcp.json`, `agents/`, `skills/<name>/SKILL.md`,
  `hooks/hooks.json` at the plugin root; plugin MCP tools are named `mcp__plugin_assay_assay__<tool>`.
  `claude plugin validate` passes (warnings: no icon, no author).

## Schema and SDK

- **Migrations use Alembic** (the user's instruction; the spec said not to). Each revision runs one plain SQL file from
  `db/alembic/sql/`, so the schema stays reviewable SQL and there is no ORM. Alembic/SQLAlchemy are imported only
  when an upgrade is needed; the version check is plain SQL (keeps CLI and hook start-up fast). Forward-only. (P7, P10)
- `task_events` has a `project_id` column (not in the spec DDL) so per-project views and export need no join.
- Project ids are slugs (`[a-z0-9][a-z0-9_-]*`, CHECK constraint). NOTIFY keeps the original channel for
  ids up to 57 bytes and uses a hashed channel for longer ids.
- **`assay_sql` isolation:** per-project copies of every `v_*` view live in schema `assay_q`, filtered by
  `current_setting('assay.project')`. `assay_reader` is a LOGIN role and `sql()` connects *as* it, so the session
  user is the reader and `set_config('role', ...)` cannot escalate. Fallback when a managed Postgres refuses that login
  (set `ASSAY_READER_PASSWORD`): `SET LOCAL ROLE` plus a keyword block list. Only SELECT/WITH/TABLE/VALUES are accepted.
  A server cursor bounds the rows sent to the SDK. The 4,000-token cap includes response metadata.
- **Canonical JSON:** integral floats become ints when hashing (`1.0` and `1` match in dedup). Experiments
  retain the original values that passed the project schema.
- **Duplicate check includes invalidated experiments** (it is the unique index). Re-running a config after fixing an
  evaluator needs a different `code_ref` (include the evaluator version in it).
- `is_new_best` is `outcome == 'improved'`, so it honours `improve_epsilon`; `v_best` ranks by raw value and can show
  a value that beat the best by less than epsilon.
- **Claim graph** (§6.1 left gaps): `inconclusive` -> supported/refuted/superseded/retracted;
  `supported` -> inconclusive/superseded/retracted as well. `proposed -> verified` needs `force=True` (two decisions
  otherwise). Terminal: refuted, superseded, retracted.
- **Rerun rule:** Verification needs valid successful runs of the same configuration and code as an original
  run cited by a supported decision or support link. The defaults are three distinct seeds and two authors
  (`rerun_min_seeds`, `rerun_min_authors`), including an independent author. Force applies only to graph transitions. (P4)
- Claim transitions, experiment appends and invalidations share the project transaction lock. The latest
  decision's evidence controls `evidence_invalidated`, with support links used when the decision has no evidence.
  Support counts and research activity views exclude invalidated experiments.
- Default identity is `ASSAY_AGENT`, else `human`. Unknown agent names are registered as workers on first use. (§7.4)
- `heartbeat` succeeds while the caller is still `lease_owner` even if the lease has run out but nobody re-claimed
  the task. `complete_task` summary is capped at 1,000 characters.
- Connection pool per database URL, closed at exit (otherwise every CLI call waited 5 s for pool threads).

## Brief

- Deterministic given (DB state, task, budget, agent, `now`); `compile_brief(..., now=, log=False)` is what the property
  test uses. Optional items are trimmed bottom-up and long header fields can be shortened to fit 105% of the
  requested budget. Requests that cannot fit the required structure return a validation error.
- "Already tried" with a task that has tags or `spec.config_space`: only items with relevance > 0 (§8.2 score); without
  them: most recent first, labelled "total".
- "Since your last brief" lists other agents' records only (your own are not news).

## Interfaces

- CLI: `--json`, `--project`, `--agent` are accepted anywhere on the command line. Usage errors exit 1 (2 is reserved
  for duplicates). Extra commands: `get`, `search`, `task heartbeat|cancel|show`, `db stop|url`.
- `assay serve` mounts the streamable HTTP MCP at `/mcp` on the viewer's port; the MCP SDK's Host-header check
  (DNS-rebinding protection) stays on.
- Plugin skills are namespaced: `/assay:assay-lead` when installed as a plugin, `/assay-lead` when copied by hand.
- Worker subagent rules (§12.2) are written in English, as a faithful rendering of the spec's six rules.
- **Hook:** Queue entries are written atomically under `~/.local/state/assay/spool/` by database target. A detached
  processor retries pending writes. Later hooks or `assay hook replay` resume processing after a longer outage.
- The hook captures subagent output that cites no existing `e:/c:/t:` id. A hash of the full report provides
  idempotence while preserving distinct reports with the same opening text.
- stdio MCP advertises its tools before opening the project. The first ledger call opens the project, allowing
  `assay init` in the same Claude Code session.

## Demo and bench

- Circle-packing evaluator: violations up to 1e-9 are accepted, but every radius is reduced by the largest violation
  before summing, so the tolerance can never add score; anything larger is invalid.
- The scripted swarm measures deadlines with `time.monotonic()`: the WSL wall clock jumped by tens of seconds during
  testing.
- The demo's rerun claims report the observed minimum for the listed seeds.
- `bench --dry` simulates agents with stated assumptions (drop rate 0.15, compaction at 3,000 tokens, 400 tokens per
  evaluation step). Its report is labelled as a pipeline check, not evidence. Real mode prints the `claude -p` commands
  unless `--execute` is passed (cost rule, §16.3).
- Real runs use a common API dollar cap and separate project IDs. Evaluation receipts identify each result across
  lead reports, files and the ledger. Token curves use reconciled usage checkpoints or a measured endpoint.
