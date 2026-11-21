# Decisions

One line per decision the spec left open or that changed during implementation. Pn = product principle.

## M0 spike (2026-11-19)

- **Embedded Postgres:** `pixeltable-pgserver` 0.7.2, PG 16 pinned (`postgres_version=16`; the wheel also ships PG 18).
  Verified on Linux x86_64 / WSL2 with Python 3.13; macOS and Python 3.11/3.12 are covered by the CI matrix,
  not verified by hand. pgvector is included, pgcrypto is not (not needed: `gen_random_uuid()` is core). (P7)
- **Server lifetime:** `cleanup_mode=None`: the first process starts the server and nobody stops it on exit,
  so one Claude Code session ending never cuts off another (`cleanup_mode='stop'` would). `benchbook db stop`
  stops it explicitly. Verified: two processes connected at once, the second keeps working after the first exits.
- **Start-up race:** the package already serialises start-up with an inter-process file lock (`fasteners`),
  so benchbook does not add its own `fcntl.flock`. All embedded-server code stays in `db/conn.py`.
- **MCP SDK:** `mcp` 2.x is current; `FastMCP` is now `mcp.server.mcpserver.MCPServer`. Pinned `mcp>=2.3,<3`.
  Expected failures are raised as `ToolError` so the model sees the message (any other exception is masked).
- **Hook input (code.claude.com/docs, 2026-11-19):** `SubagentStop` carries `last_assistant_message` (the
  subagent's final text). When a subagent reports through `SubagentHandback`, the report is in
  `tool_input.message` of a `PostToolUse` event for that tool. The plugin hooks both; the handler also reads
  `PostToolUse` on `Agent` (`tool_response` text blocks) if wired manually.
- **Plugin format:** `.claude-plugin/plugin.json`, `.mcp.json`, `agents/`, `skills/<name>/SKILL.md`,
  `hooks/hooks.json` at the plugin root; plugin MCP tools are named `mcp__plugin_benchbook_benchbook__<tool>`.
  `claude plugin validate` passes (warnings: no icon, no author).

## Schema and SDK

- **Migrations use Alembic** (the user's instruction; the spec said not to). Each revision runs one plain SQL file from
  `db/alembic/sql/`, so the schema stays reviewable SQL and there is no ORM. Alembic/SQLAlchemy are imported only
  when an upgrade is needed; the version check is plain SQL (keeps CLI and hook start-up fast). Forward-only. (P7, P10)
- `task_events` has a `project_id` column (not in the spec DDL) so per-project views and export need no join.
- Project ids are slugs (`[a-z0-9][a-z0-9_-]*`, CHECK constraint). NOTIFY keeps the original channel for
  ids up to 57 bytes and uses a hashed channel for longer ids.
- **`benchbook_sql` isolation:** per-project copies of every `v_*` view live in schema `benchbook_q`, filtered by
  `current_setting('benchbook.project')`. `benchbook_reader` is a LOGIN role and `sql()` connects *as* it, so the session
  user is the reader and `set_config('role', ...)` cannot escalate. Fallback when a managed Postgres refuses that login
  (set `BENCHBOOK_READER_PASSWORD`): `SET LOCAL ROLE` plus a keyword block list. Only SELECT/WITH/TABLE/VALUES are accepted.
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
- Default identity is `BENCHBOOK_AGENT`, else `human`. Unknown agent names are registered as workers on first use. (§7.4)
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
- `benchbook serve` mounts the streamable HTTP MCP at `/mcp` on the viewer's port; the MCP SDK's Host-header check
  (DNS-rebinding protection) stays on.
- Plugin skills are namespaced: `/benchbook:benchbook-lead` when installed as a plugin, `/benchbook-lead` when copied by hand.
- Worker subagent rules (§12.2) are written in English, as a faithful rendering of the spec's six rules.
- **Hook:** Queue entries are written atomically under `~/.local/state/benchbook/spool/` by database target. A detached
  processor retries pending writes. Later hooks or `benchbook hook replay` resume processing after a longer outage.
- The hook captures subagent output that cites no existing `e:/c:/t:` id. A hash of the full report provides
  idempotence while preserving distinct reports with the same opening text.
- stdio MCP advertises its tools before opening the project. The first ledger call opens the project, allowing
  `benchbook init` in the same Claude Code session.

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

## Step 3: autoresearch (2026-11-20)

- **A0:** the SLSQP `repair()` fix was already on main (331e488). The distribution name, the README install
  commands and the `v0.1.0` tag wait for a human decision (MANUAL_QA.md); A1-A9 went ahead as §15 allows.
- **Migration 0005** adds `settings_events` and `blobs` (append-only, notify on settings changes), appends a
  `partition` column to `v_leaderboard`, `v_best`, `v_tried`, `v_progress` (`seed` too), and adds
  `v_partition_best`, `v_lineage`, `v_inflight`, `v_knob_history`. All `benchbook_q` reader copies are dropped and
  rebuilt (they are `SELECT *` snapshots). Without `settings.partition_key` the partition expression is NULL and
  every view behaves as before.
- **Partition-aware outcome (C1):** the epsilon is `settings.partitions[p].epsilon`, else `improve_epsilon`; both
  are re-read inside the writer's lock because calibration runs in another process. A new setting
  `outcome_baseline = "verified"` (set by `ar init`) compares with the best verified mean of the partition and
  falls back to the raw best before anything is verified. Without it the v0.1 raw-best rule applies.
- **update_settings** is a JSON merge patch (RFC 7386), orchestrator/human only, one `settings_events` row each.
- **Intents (C3)** are tasks inserted as `claimed` by their creator (`max_attempts` 1, tag `intent`). Exclusivity
  is generic: `create_intent(..., exclusive=[field])` refuses a live intent with the same `spec[field]`, under an
  advisory lock. `ar` uses `knob_key = partition|canonical knob changes` (claim) and `code_key =
  partition|code hash` (run). New error `Conflict` maps to exit code 2.
- **next_task** gained `spec_match` (jsonb containment) next to `exclude_agents` (C4): replication tasks are
  only taken by agents in the same partition.
- **ml-research pack:** `ok_metrics_required = ["val_bpb"]` is checked in code (a JSON Schema cannot see
  run_status). Packs can name a `brief_compiler`; the ml-research brief replaces the default one.
- **T5 is stricter than §10.2 literally:** verified needs the mean of all runs *and* the mean of the replications
  alone to beat the verified best by epsilon. With only the 3-run mean, one lucky first run (−0.01 in the
  simulator) lifts the mean past a small epsilon and breaks the §12 condition that lucky candidates end refuted
  or inconclusive. The first run was selected for being good, so it should not count twice.
- **Calibration doubles as the baseline's replication:** the first code of a partition is re-run twice (T1); when
  done, T2 stores sigma/epsilon and T5 verifies that code without a comparison, so the partition gets its first
  verified best. A candidate that finishes replication before any verified best exists is verified on its
  own replication. Calibration with fewer than 3 successful runs stores `status: partial|failed` and the floor.
- **replicate_max_queue counts candidates** (default 4), not tasks. A better arrival cancels the open tasks of the
  weakest candidate that nobody is running; otherwise the arrival is closed as `inconclusive` (oracle none)
  without replication. T3 also closes candidates already beaten by a verified best recorded since.
- **T7** compares the first run with "new best + epsilon" exactly as §10.2 says, and only cancels open
  (unclaimed) replication tasks; T5 then judges the candidate `inconclusive`.
- **Result claims** are proposed by the referee with the first run as `supports` evidence, then decided
  `supported` (oracle metric, evidence: first run) and `verified` (oracle rerun, evidence: all runs). Statements
  include the experiment's short id, so they never collide. A rerun-rule rejection becomes `inconclusive`.
- **ar run refuses failed code too** (a crash usually repeats); `--retry-failed` re-runs code that has only
  failed runs. A run whose record cannot be written is queued in `.benchbook/ar_pending.jsonl` and written by the
  next `ar` command, which is how "every started run is recorded" holds when the database blips.
- **Parent code:** compared by normalised hash, so a comment-only commit after a run still counts as "committed".
  A run whose parent hash equals its own code (a re-run of the parent) records no parent.
- **Hardware class:** vendor words, memory types and the word "gpu" are dropped; form factors (pcie, sxm4, laptop)
  are kept because they change speed. Memory comes from the name, else memory.total rounded to GB.
- **program.benchbook.md:** the §9 loop block is kept verbatim (Korean comments, as the spec requires) and followed by
  an English rendering; the rest is upstream `program.md` text adapted to the shared loop. MIT attribution in
  an HTML comment at the top (upstream has no LICENSE file; its README states MIT).
- **ar import (Should, S3)** is implemented: rows are first runs whose code comes from `git show <commit>:train.py`;
  improvements become candidates and get replicated. Re-importing is a no-op.
- **Not done (Should):** the LLM research-director mode, the `--trust candidate` ablation, `ar init --shim`, and
  hands-on MLX/macOS fork checks. `sync --trust candidate` itself exists.
- **`python -m benchbook`** was added, and `python -m benchbook.cli` now dispatches through the canonical module: as
  `__main__` it used to hold a second global STATE, so `--json`/`--project` were ignored by `benchbook ar`.
- **Simulator:** noise is drawn fresh per run (the real `train.py` fixes seed 42), runs sleep instead of training,
  and they print `sim_run_id`, `true_bpb` and `lucky` so the e2e can check that nothing started went unrecorded.
  The e2e uses a higher lucky rate (0.15) than the default (0.02) so a 1-minute run exercises refutation.
- **Bench:** arm A runs are collected by a `uv` shim on PATH (wrap_train.sh); arm A's "false best" is a keep
  whose 3-run re-run mean is worse than its reported value by more than B's calibrated epsilon. `--sim` numbers
  are dominated by CLI start-up (~1-2 s per `benchbook` call against 0.2 s simulated runs) and say nothing about
  real 5-minute runs; the report labels them as a pipeline check.

## Review fixes (2026-11, benchbook-review.md)

- **Rerun compares values (C1).** `verified` by `rerun` now also needs the mean of all evidence runs and the mean
  of independent reruns (runs by agents other than the originating author) to beat a baseline by epsilon, the same test the
  autoresearch referee applies. Baseline: `payload.baseline` if numeric, else the best earlier ok result of another
  dedup_key in the original's partition. No baseline means refusal, except for claims with
  `payload.measurement: true` (including calibration claims). Measurements and result claims must state the
  evidence mean in `payload.mean`. Epsilon is the larger of the partition epsilon and `payload.epsilon`.
  Settings are re-read inside the decision transaction (C7).
- **No auto-verify without a baseline (A2).** A candidate whose replications finish while its partition has no
  verified result is left unjudged. A calibration whose verdict is not `verified` triggers another calibration
  round of the same baseline code with fresh seeds (`spec.round`, at most `calibration_rounds`, default 3). Result
  claims, tasks and verdicts are keyed by (origin, round).
- **Distinct replicators (A3).** Tasks may carry `spec.distinct_agents_for = <spec key>`; `next_task` skips a task
  when the agent already claimed another task with the same value of that key. Claims by one agent in a project
  are serialized. The referee groups replication
  tasks by `replication_group = <origin>#<round>`, and a verdict whose replications share an author or include the
  original author is `inconclusive`.
- **TRUNCATE and agent roles (C2).** Statement-level TRUNCATE triggers on every ledger table; agents rows cannot
  change role, id or project, nor be deleted. Owner privileges (DISABLE TRIGGER) remain open until v0.2 splits the
  writer role.
- **benchbook_sql (C4).** Migration 0006 revokes EXECUTE on `set_config` from PUBLIC and grants it to the migrating
  role. This is database-wide: other non-superuser roles in the same database lose `set_config` too (use `SET`),
  which is acceptable for a database dedicated to benchbook; on a managed database where the migration cannot do it, a
  NOTICE is raised and the regex plus the post-query check remain. The SET ROLE fallback is opt-in
  (`BENCHBOOK_SQL_ALLOW_SET_ROLE=1`).
- **Unique index (C7).** Two partial indexes replace `coalesce(seed, -1)`: one over non-NULL seeds, one for NULL.
- **Candidates (C7).** `v_partition_best` candidates are the first ok run of their config, whatever the seed.
  The autoresearch referee still only replicates seed-0 runs, which is what `benchbook ar run` records.
- **CLI identity (I1).** `Ledger.open` has no default agent. CLI commands that only read fall back to `human`
  (nothing is written in its name); commands that write need `--agent`, `BENCHBOOK_AGENT` or, for `decide`, `--as`.
- **Task linkage and leases (U3, U4).** `record_experiment` without `task_id` links the one non-intent task the
  agent holds a live lease on; with several, the CLI warns. `task claim <id>` claims a specific task.
  `settings.lease_seconds` sets the live default for claims and heartbeats (900).
- **Worktrees (U6).** When no `.benchbook/config.toml` is found upward, the main worktree's copy is used
  (`git rev-parse --git-common-dir`).
