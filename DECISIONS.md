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

## Name (2026-11-21)

- **benchbook.** The notebook kept next to the bench. `assay` was dropped because four PyPI packages (`assay-ai`,
  `assay-it`, `assay-kit`, `assay-sdk`) install a top-level import `assay`, three of them install an `assay`
  command, and they describe themselves with the same words (AI evidence, verification layer). `benchbook` was
  free on PyPI, npm, crates.io and as a GitHub account on 2026-11-21. Weak point: it can be read as a benchmark
  tool, so the README puts "your agents' lab notebook" next to the name. No trademark search yet; one is done
  before the names are reserved (docs/releasing.md).
- **Never move a user's folder on upgrade.** A server or a hook may be using it. A pre-rename location (config
  folder, embedded cluster, state directory) stays in use while the new one has no data, and doctor prints the move.
- **Accept old names everywhere they are persisted; write only new ones.** Blob URIs, transcript markers, the
  viewer cookie, bench keys. The exception is the auto-capture hash domain, which keeps `assay:auto-captured`
  forever so stored hashes still match.
- **A server started as `assay mcp` keeps the `assay_*` tool names.** The old plugin's agent definition and
  permission rules name them; updating the plugin switches both.
- **Old-name notices never reach stderr on the hook path.** They wait in memory and go to `hook.log`, and only for
  a worker's event.
- **Renaming existing databases is a bootstrap, not a revision.** Legacy databases (0007 at most) are renamed in
  place before Alembic runs. The step is keyed on schema names, so Alembic continues from the database's own
  revision. Function bodies are re-created from the newest definition in the SQL files up to that revision, so
  they cannot drift from a new database (tests assert `pg_dump` equality). Roles are cluster-wide: only the old
  role, rename it; both, grant the new one and revoke the old; only the new one, re-grant. `pg_shdepend` reports
  other databases that still use the old role. Statements that need an administrator run first; if refused, the
  whole transaction rolls back and the exact SQL is printed. Anything else in the catalog that still names the
  old schema blocks the commit. Row data is never rewritten. `LOCK_KEY` keeps its pre-rename value so old and
  new binaries serialise.
- **Notification channel.** `benchbook_` + project id up to 53 bytes, the hashed form above that (Postgres limits
  a channel name to 63 bytes).

## Core for the verification ledger (2026-11-21)

- **Dedup mode is the pack's (2-C1).** `dedup_mode` in pack.toml: `refuse` (default) or `notify`. The refusal was
  also the unique indexes, so migration 0009 adds `experiments.duplicate_of` and narrows both indexes to rows
  without it; a trigger accepts `duplicate_of` only when it names the original ok run of the same project,
  dedup_key and seed. The database cannot read pack.toml: which projects may declare repeats is enforced by the
  SDK only.
- **Repeats point at the first run (2-C1).** Every repeat's `duplicate_of` is the one original, never the
  previous repeat. A repeat is a valid run: it counts in `v_tried` (`attempts`, `ok_runs`; not `seeds`), can be
  `v_best`, and is never a `v_partition_best` candidate. In notify mode a repeat of an invalidated run is stored.
- **`dedup_hits.kind = 'notified'` (2-C1).** A recorded repeat is not a `rejected` hit, so `v_dedup_savings`,
  `v_agent_stats` and "duplicates blocked" still count refusals only.
- **Identity (2-C3, migration 0008).** The session setting carries the token itself and the triggers hash it again
  on each row. A name in a setting is never trusted, and the identity lasts one transaction, so pooled
  connections cannot leak it.
- **Trust boundary (2-C3).** A session that may update `agents.token_hash` (the owner, a superuser) is trusted;
  every other session needs a token. Enforcement is chosen by which login a machine is given: the owner URL is
  local mode as before, a `benchbook_writer` login is enforced. An owner connection can still write as anyone.
- **Mismatching author (2-C3).** It is refused, not rewritten; a missing author is filled from the token. (The
  plan said the trigger replaces the author; refusing lets a client see its forgery attempt fail.)
- **The writer's grants are explicit, table by table (2-C3).** A later migration that adds a table must grant it
  to the writer, and a test pins this.
- **Imports are first runs only (2-R1).** An imported record always has replication number 0 and is never imported
  twice for the same hardware class, whoever imports it. No log can stand in for an independent re-run.
- **The evaluator version is bound through the partition, not `code_ref` (2-R1).** `code_ref` stays the
  `train.py` hash so live re-runs can match imported originals under the rerun rule. The `prepare.py` hash names
  the partition and is stored in `trace_ref.evaluator`. (The plan said to put the evaluator version into
  `code_ref`; that would make imported runs impossible to verify.)
- **A measured run without recorded code is `invalid` (2-R1).** It is kept as a record that the approach was
  tried, but it is never a candidate or a baseline.
- **`ar gate on` adds no new mechanism (2-R1).** It restores the existing gate settings where they are missing or
  weaker, and leaves stricter values and an explicit `time_budget_s = 0` alone.
- **exp run duplicates (2-C2).** Refuse mode blocks a same-key, same-seed earlier run before it starts: ok runs
  (invalidated included, the unique index counts them) always, failed-only runs unless `--retry-failed`. Notify
  mode runs it and reports `duplicate_of`.
- **exp run output (2-C2).** The command's output is unchanged; exp run adds one `benchbook: ` line (or a JSON
  line with `--json`) after it and puts notes on stderr. Exit code is the command's, 124 on timeout, 128+N on
  signal, 2 for a duplicate, 1 for a failed write (output log kept, no local queue).
- **exp run metrics (2-C2).** Only existing formats are read: `parse_summary` lines and a last-line JSON object,
  where `value` stands for the primary metric (the bench receipt) and `seed` is not a metric. The command gets
  `BENCHBOOK_EXP_CONFIG` and `BENCHBOOK_EXP_SEED`.
- **The benchmark keeps sessions (0-4).** It no longer uses `--no-session-persistence`. The stream's output counts
  are taken at the start of each response, so only transcripts reconcile all four token classes; the input
  classes reconcile from the stream alone. Side effect: the SubagentStop hook can now verify citations, so
  configuration C's ledger differs from the first benchmark's.
- **Cost verdicts are computed (0-4).** In `cost-breakdown.md` a hypothesis is supported when its mechanism
  explains at least 50% of the gap it targets and contradicted below 20%. Near configs use a relative tolerance
  per numeric key (25% decides hypothesis 2, as proposed in the plan). "tokens total" in report.md stays the
  lead's own `result.usage`; whole-session totals (`modelUsage`) are in `cost-breakdown.md`.

## Code pack (2026-11-21)

- **check verify runs the merge-base's declarations (1-1)**, never the commit's or the verifier's checkout's.
  Guard and `checks_changed` use the same merge-base; with no `[checks]` there, verify refuses. No
  `[guard] paths` means the default `tests/**`, `**/conftest.py`, `.benchbook/config.toml`.
- **Guard diff uses `--no-renames` (1-1)**, so a test moved out of a guarded folder shows as the deletion it is.
- **Recording (1-1).** The completion claim is proposed in `--reported-by`'s name (one per piece and commit, tags
  `piece:`/`commit:`); the attempt is by the verifier (`verifier@<host>`, harness role, by default), which must
  differ from the reporter; a supports/refutes link joins them. An interrupted run is recorded with run_status
  error and no link. Diff text and check output are never stored.
- **Of `--report-file` (1-1)**, only sha256, size and the declared check names it mentions are kept.
- **Packs can declare `[project]` defaults (1-1)** (metric, direction, settings) that `init --pack` applies. The
  code pack partitions by piece, which keeps the false-completion query within `v_leaderboard`'s
  50-per-partition cap.
- **Failure signature (1-1, rough until signature.py).** `check:first failing JUnit test:exception type`,
  `check:timeout`, or `check:exit N:<sha256 of the last output line with digits zeroed>`.
- **The verify rule is the pack's (2-P0a).** `[verify] rule`: `replication` (default, unchanged) or `all_pass`.
  `min_authors` counts the implementer and must be at least 2; the loader refuses anything that would let an
  implementer verify alone.
- **Who the implementer is (2-P0a).** The claim's author and `payload.reported_by`. The author is left out only
  when a lead, person or harness names a different reporter. The implementer's attempts never count, and it
  cannot decide `verified` on its own completion by any oracle. In local mode names are self-declared, so this
  cannot stop one process using two names; tokens on a writer login do.
- **Integrity flags and failures are sticky per commit (2-P0a).** Any valid finished attempt of the piece at the
  commit that failed, touched the guard or changed `[checks]` keeps the rerun oracle out, cited or not.
  Invalidating that attempt is the way back.
- **Disagreement is inconclusive in either direction; a lone failing re-run decides nothing (2-P0a)**, because
  `refuted` is final and flakes exist. (The plan named only the implementer's passing record.)
- **verify writes supported then verified after a dry run (2-P0a)**, keeping the state graph instead of using
  `force`.
- **Under a token the verifier records the claim (2-P0a)**, because a token is one agent. The reporter goes in
  `payload.reported_by`.
- **A piece is a task (2-P1b, open question 2).** It is an intent tagged `piece` and `piece:<name>`, with spec
  `{piece, paths, declared}`. There is no new table, status, queue or migration, and `v_inflight` already
  exposes paths and module tags.
- **Paths are expanded once, at start (2-P1b).** Tracked files a glob matches are stored escaped; a glob matching
  none, or more than 200, is stored as the glob. New files beside files a glob matched are not occupied.
- **The overlap test is exact (2-P1b).** It returns a witness path both patterns match. An unreadable stored
  pattern counts as overlapping. It runs inside the intent advisory lock, so of concurrent starts one wins.
- **Lifetime (2-P1b).** The lease is 7200 s. Restarting by the same agent renews it in the same task (`--paths`
  replaces the paths). `piece done` by the holder marks the task done or failed; by a lead it cancels another
  agent's piece. Expiry frees the paths. `check verify` does not release.
- **Findings use existing claim kinds and statuses (2-P1c).** A finding is an `observation` tagged
  `out-of-scope`; accepted is `supported` with the issue as a cited source, rejected is `refuted`. The report is
  the evidence, as a source keyed by its digest, so `n_support` counts the reports. Open means `proposed` or
  `inconclusive`.
- **The finding dedup key is file + normalised sentence (2-P1c)**, under the fixed domain
  `benchbook:out-of-scope\0`, without the author. Reports from different subagents or tokens merge, and the
  stored hash never changes between releases.
- **Findings are parsed in the background processor, not the hook process (2-P1c).** The heading is stored with
  the queued report so a replay reads the run's setting, and the hook process gains no work or imports. No
  `mod:` tag is inferred from a path; it appears only when the item writes it.
- **`finding accept` exists only because no CLI could create a source (2-P1c).** Rejecting needs no new command.
- **check run records through the spool, never inline (2-P0b).** The wrapper's 300 ms budget leaves no room for
  the ledger imports, and a database outage must not touch the exit code. The gate counts passing runs still
  waiting in the spool.
- **Implementer runs are marked (2-P0b).** `metrics.via` (`check-run`, `bash-hook`) marks a run in the
  implementer's own checkout, never an independent re-run, whoever recorded it (the hook records as `hook`); the
  all_pass rule skips such runs when counting independent passes and still lets their failures block.
  `worktree_dirty` marks a run that did not test `code_ref`: it is no attempt of that commit, and the gate
  ignores it.
- **Hook matching is word equality (2-P0b),** after dropping quotes, leading assignments and redirections, with
  one leading `cd DIR &&` and optional `{junit}`, from the repo root. Pipelines and lists are observed but not
  recorded; `check run` calls are observed and recorded once, by the wrapper.
- **The gate fails open (2-P0b)** on no project, no checks, no piece (including the base branch), no commit, a
  project outside the code pack, or any error.
- **Open question 1 (2-P0b).** Per the Claude Code hooks documentation, SubagentStop can block (exit 2 or
  `decision: "block"`). Not measured; the plugin gates on TaskCompleted only.
- **The SWE-bench task set comes from the harness's copy of Verified (1-2).** swebench 5 evaluates from
  `SWE-bench/SWE-bench_Verified` (image, eval_script, log_parser), so selection reads that copy and checks
  princeton-nlp's ids, labels and base commits against it.
- **Visible check = the hidden eval script without the test patch, on the PASS_TO_PASS tests that exist before it
  (1-2).** It is fixed per instance in `instances.json`; a preflight confirms it, and failures are replaced from
  same-repo reserves.
- **A harness error is not a false completion (1-2).** `swe_check` exits 3, the attempt is invalidated, and the
  batch stops until the harness is fixed. "Reported done" that cannot be verified (missing commit, history that
  does not descend from the declarations) is listed beside the rate, not inside it.
- **Benchmark arms exclude account connectors (0-4).** Configuration C needs the plugin's MCP server and so cannot
  use `--strict-mcp-config`; the connectors of the account running it were loaded with it (8 tools in the first
  batch, 90 in the rerun). `ENABLE_CLAUDEAI_MCP_SERVERS=false` is set for every arm.

## Plan execution (2026-11-21)

- **Phase 2 was built before the phase 1 verdict.** The plan gates the code pack's P0/P1 and the research adapters
  on the two phase 1 numbers. On 2026-11-21 the owner asked for all of phases 0 to 2 to be built at once, so both
  sides exist on the `benchbook` branch; the verdict (bench/results/2026-11-21-phase1/report.md) decides what is
  kept in the release and published.
- **Benchmark baseline.** The 2026-11-21 batch at commit `d65441a` is the baseline that later changes are compared
  with, with C from the runs that had the account's connectors switched off. The first batch's C had 8 connector
  tools in its lead and is kept as it was.
- **Cost findings that change the plan's assumptions.** C is 4.2 times A in session tokens and 1.8 times in cost,
  not about 5 times; hypothesis 2 (near configurations) is contradicted in both batches, so no near-config notice
  is planned; the gap is made of model calls (111.7 against 36.0 per run) that each re-read their context.
- **The harness is named in the hook command, not guessed (2-P1d).** `--harness` in each manifest; Codex and
  Claude Code inputs look alike (`Bash`, `tool_input.command`).
- **No exit code, no record (2-P1d).** A run whose outcome the input does not give (Codex `exec_command`, a Cursor
  `postToolUseFailure` error, a wrapped Gemini `llmContent`) is observed as `unrecordable`, never recorded as a
  pass. Most Codex runs are therefore observed only; the wrapper (`check run`) stays the canonical path. The
  fixtures are built from the harness documentation, not captured from real runs.
- **No completion gate outside Claude Code (2-P1d).** Neither Codex nor Cursor documents a task-completed event;
  SubagentStop "block" and `followup_message` only continue the agent.
- **Foreign transcripts are not read (2-P1d).** Codex and Cursor reports skip the citation check and are always
  captured as notes.
- **Doctor compares input shape, not version numbers (2-P1d).** Only Cursor's input has a version.
- **One signature module (2-P0c).** JUnit, then tsc, then exit code plus normalised last error line, then timeout.
  Several failing tests or errors add a count and a set hash, so different sets never merge. Numbers outside
  words are blanked; the words stay. The fallback is heuristic: two failures whose last error line differs only
  in numbers merge, so JUnit is recommended.
- **Matching is per check element (2-P0c).** Signatures that share a `check:part` element on the same piece are
  "seen before". It is a notice only.
- **check run looks up only after a failure (2-P0c),** in a 2 s thread with one plain connection, before the run
  is queued, and never starts the embedded server. The passing path is unchanged (about 65 ms added; a failing
  run with the ledger up about 210 ms).
- **Discarded approaches (2-P0c).** One per (piece, signature, approach) under the fixed domain
  `benchbook:discarded\0`; later failures add evidence. Runs with uncommitted changes leave none. The claim
  survives verification. Writing it never raises, so the spool never records an attempt twice.
- **The code brief has no `--pack` (2-P0c);** the project's pack chooses the compiler. Tags are a union; `file:`
  matches by prefix; pieces are found through their `piece start` task. Other packs refuse `--tags`.
- **"Reads that changed a plan" is an upper bound (2-P0c).** `brief_log` does not record what a brief showed, so
  the count is citations of claims that a brief could have shown.
- **CI re-runs rather than reading other workflows' results (2-P1a).** The action runs `check verify` itself, so
  a CI attempt has the same shape and rule as a lead's. (The plan's wording was to read check-run results with
  `gh`.)
- **CI never reports a completion (2-P1a).** It links to an existing claim (`--reported-by` its reporter); with
  none, it records only the attempt and the piece shows "not reported". A pull request rebased after the lead's
  verify has new commit ids, so its pieces show "not reported".
- **The summary's "verified" is the rule, not the decision (2-P1a).** It uses the ledger's own rule check on the
  recorded runs and shows the claim's status beside it; the precedence is verified, then disagrees, then claimed
  only.
- **CI identity (2-P1a).** Owner URL: self-declared `ci:<run_id>`, a worker. Token: the token's agent for every
  run (a token is one agent), with the run in `trace_ref`. The job that holds the credential runs the pull
  request's code, so a harness token is recommended and CI passes `--no-decide` unless asked and holding a lead
  token.
- **Only the configured author's marked comment counts (2-P1a)**, and extra copies are deleted. Pull requests with
  no pieces get no new comment. Forks and Dependabot pass with a notice, since they get no secrets; a missing
  secret on a same-repository pull request fails.
- **The action installs benchbook from its own tree (2-P1a)**, built as `0.0.0+action`, so the commands match the
  action. It has not run on a real GitHub runner yet.
## After the independent review (2026-11-21)

A review of the merged token and verification code reproduced five holes; these are the decisions of the fixes.

- **A claim's identity is (piece, commit, reporter).** `statement_hash` is client-supplied and unique per project
  only, so it cannot carry identity. On a clash verify writes a statement that names the reporter, and as a last
  resort a random hash. A server-side hash check would refuse the callers with their own hash and would not stop
  pre-emption.
- **Union of implementers by trusted reporter.** Every completion claim of a piece and commit adds its reporter to
  the implementers; `payload.reported_by` is trusted only on claims a lead, a person or a harness agent recorded,
  and a worker's `reported_by` stays on its own claim. Sets never shrink, and a worker cannot bar the lead or CI
  from deciding by naming them.
- **`junit = true` means "a test ran".** Exit 0 with no report, no testcase or only skipped ones is a failed
  check. `check run` keeps the command's exit code and records the failure.
- **Lease cap and `worker_intents` are enforced in `guard_task`; path overlap stays in the SDK.** Migration 0008
  was edited in place because nothing with it has been released.
- **The credential scrub is limited to `check verify`.** It removes the inherited copies; code under test runs as
  the verifier's user and can still read what that user can, so it is hygiene, not isolation.
- **Still not prevented.** A fake JUnit report written by the code under test; a hook-recorded run that exits 0
  with nothing run counting for the completion gate (never as an independent re-run); a raw writer taking a piece
  name or paths up to the lease cap; self-declared names in local mode.
