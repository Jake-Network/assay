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
- **Still not prevented.** A fake JUnit report written by the code under test; a raw writer taking a piece name or
  paths up to the lease cap; self-declared names in local mode. (A hook-recorded run that exited 0 with nothing run
  was closed by the second review, below.)

## After the second review (2026-11-21)

A second review reproduced six more holes (`tests/test_second_review.py`).

- **An attempt says what was declared.** check verify stores `checks_declared`, the merge-base's check names. An
  attempt is *complete* when it ran all of them; only complete attempts are evidence under all_pass and in the PR
  summary. An attempt without the field (hand-recorded, or from before) is partial: the ledger cannot know what was
  declared, and a missing field must not read as "everything". A failed check in a partial run still blocks, and a
  partial pass does not make a full failure "disagree" (no `inconclusive` from it).
- **The spool is keyed by writer.** The author of a spooled entry is decided at processing time from the
  environment (the token), so the entry has to be bound to the token it was queued under. A per-token spool
  directory keeps two agents' processors apart by construction; the digest in each entry (`writer`) makes a
  misplaced entry wait instead of being recorded under the wrong name. Without a token the key is unchanged, so
  queued entries from before are found. The token itself is never written; the digest has its own domain.
- **The hook reads the report the command named.** The hook cannot see a report the command did not write, so a
  junit check run without its `{junit}` word is observed, not recorded (neither pass nor fail): it is no evidence
  either way, and recording it as a failure would block the commit's verification. A report older than the command
  (by more than 120 s of slack, for coarse or stepping clocks) is ignored.
- **A lease is renewed only while live.** Checked in the UPDATE itself (`lease_expires_at > now()`), so it is atomic.
  Not added to `guard_task`: the path overlap it protects is checked in the SDK only (above), so a database rule
  for renewal alone would not stop a raw writer.
- **CI names the claim.** `pr pieces --claims` and check verify used different lookups; the claim id now travels
  from one to the other, and verify checks project (the id resolves only in its project), piece, commit (tags and
  payload) and reporter. verify's own lookup also stopped requiring one of its two statements: identity was
  already (piece, commit, reporter).
- **The runner waits for the group.** Signal 0 to the group, reaping the shell meanwhile (its zombie keeps the group
  alive), then SIGKILL after the grace time. Only after a timeout or an interruption: background processes a
  command leaves behind after a normal exit are still not killed.

## After the third review (2026-11-21)

A third review found six gaps in the fixes above (`tests/test_third_review.py`).

- **"This run's report" is mtime after the start, used once.** The hook fires after the command, so it cannot
  compare before and after; a PreToolUse hook could, at the cost of a second hook on every shell call. The start is
  the hook's time minus the duration the harness reports, with 1 s of slack for coarse mtimes; a report already
  read for a recorded run is refused (fingerprint: path, inode, size, mtime in ns). Without a duration the run is
  not recorded. A wall clock that steps backwards during the run can still let an older report through; one that
  steps forwards refuses a fresh one (fail-safe).
- **Renewal and occupation share one lock.** heartbeat now takes the intent advisory lock, and it and update_intent
  read the expiry with `clock_timestamp()` after it: `now()` is the transaction's start, which can be before the
  lease ran out. Every heartbeat takes the project-wide lock; it is held for one UPDATE.
- **A partial pass is not linked.** Leaving it unlinked keeps the documented SQL unchanged and makes `rerun` mean
  "has a linked re-run" as before; a failure is linked whatever was run.
- **Expiry and release are told apart by `lease_expires_at`.** next_task fails an expired intent without touching
  it; complete_task and cancel_task clear it. No new column or event is needed.
- **Rotation is bridged by what the ledger said earlier.** A rotated token cannot authenticate, and the ledger keeps
  only the current hash, so whose the old spool is comes from a note the processor wrote while the old token
  worked (`writer.info`: target, writer digest, agent per project). It is a local file: like the entries
  themselves, it guards against mix-ups between agents sharing a state directory, not against one of them editing
  files. Entries of a token that never reached the ledger stay where they are.
- **Matcher.** `parts.pop()` on the right-hand side ran before the assignment's index, so the word before the
  option was replaced; the test now goes through `match()` with the declaration.

## After the fourth review (2026-11-21)

Five gaps in the third round's fixes (`tests/test_fourth_review.py`).

- **Before and after, not a clock.** Any time window lets in a report written just before the command. A
  PreToolUse hook (`bash-start`) now notes the report's state under the tool call's id and the PostToolUse hook
  requires a different state afterwards. This costs a second hook on every Bash call, the same TOML read the
  result hook already pays; on any other command it writes nothing. Codex, Cursor and Gemini get no PreToolUse
  manifest here, so they cannot record a passing JUnit run through the hook (`check run` still can). A process
  outside the command that rewrites the report while it runs is still taken for it.
- **One lock from reading the report to marking it used.** An flock in the spool; held only for junit checks
  that exited 0.
- **Lease arithmetic after the lock.** create_intent, update_intent and the overlap checks use `clock_timestamp()`
  as heartbeat already did: under the intent lock, `now()` can be older than the lock by however long it waited.
- **A failed renewal looks before restoring.** A LeaseLost renewal without `--paths` restores the paths only if the
  intent actually ran out (the same test as `_expired`); a release or a takeover is an error that asks for --paths.
- **Every token's spool knows its target from the start.** `writer.info` is created with the target and writer
  when the first entry is queued (no ledger needed); the agent is added once a ledger confirms it. A replay then
  sees every queue on its target, and counts the ones it cannot attribute as `held`, which makes it fail.
- **Adoption is idempotent.** The copy's name derives from the original's spool and file name; a copy is written
  only when neither it (queued, dead or quarantined) nor a record of its processing (`adopted.done`, written by the
  processor before it removes the copy) exists. The old spool's lock is taken (without waiting) while moving from
  it.

## Boundary mode, T01-T16 (2026-11-22, "benchbook swarm과 1M 너머")

- **T01 pin metrics only on pinned runs.** `checks_source`, `checks_pin` and `checks_sha256` are written only when
  the checks came from a pin; a run against a committed config keeps exactly the metrics it had, so the false
  completion query and older readers see no new keys.
- **T01 a re-pin is compared by hash.** Every pin is a new claim (unique dedup hash), so the newest is always the one
  in effect. "A new pin after the first attempt" counts as a `[checks]` change only when the pin in effect at the
  first attempt is missing or has another sha256: re-pinning the same declarations does not send a piece to a person.
- **T01 the working tree is compared only for config pins.** A pin taken with `--check` never came from
  config.toml and is not compared with it; a pin taken from config.toml is.
- **T01 init keeps config.toml.** `write_config` used to overwrite the file; it now rewrites only the `project`
  line of an existing one, so `init --pack code` can pin the `[checks]` already declared there.
- **T03 an environment failure needs a pass elsewhere.** The spec counts any check that fails the same way at the
  merge-base as an environment failure. A piece's goal check fails at the merge-base by design (a test written
  before the fix, SWE-bench's FAIL_TO_PASS tests), so that rule would never refute a commit that leaves it failing,
  and the false completion rate would lose exactly its subject. The rule adds the evidence that tells the two apart:
  the implementer's own clean run of the commit (`via`) passed the check. Passing there and failing in the clean
  worktree the same way as at the merge-base is the environment's signature. The merge-base run happens only then.
- **T03 "the same way" includes subsets.** Besides an equal signature element, a JUnit check whose failing tests all
  fail at the merge-base too is the same failure (the base can fail more).
- **T03 a commit equal to its merge-base is not compared** with itself.
- **T04 conftest.py is never "added only".** Lines added to a conftest.py (an autouse fixture, a collection hook)
  can weaken every test without deleting any, so any change to one is a weakening, as the mutation table expects.
- **T04 guard_weakened is always written** (a boolean on every attempt, like guard_touched); the file lists and the
  test counts only when there is something to say.
- **T04 names added to an import are lines added (2026-11-25, after the boundary pilot).** In arm F three of four
  merged pieces passed their re-run and still went to a person, because each rewrote one line of an import to add
  a name (`get_path)` as `get_path,` + `run_length)`, and `..., Bits` as `..., Bits, weighted_median`); that is
  most of why F's lead knew 1 of 10 verified pieces. A modified Python test file whose deleted lines all sit inside
  import statements, none on a line shared with another statement or a compound statement's header, and that still
  imports every (module, level, name, alias) it imported before, now counts as appended. The handoff's textual
  rule (drop whitespace and a trailing `,`, `)` or `\\`, then match an added line) was not taken: it also passes
  `assert f(1)` rewritten as `assert f(1,` + `2)`, an argument added to a call under an assert, and needs another
  rule for one-line imports. Parsing both versions closes that and covers `from x import a` → `from x import a, b`.
  Other languages keep the old rule (a rewritten line is a weakening), and a file that does not parse at either
  commit is weakened. The rows are in tests/test_mutations.py (`IMPORT_ROWS`, and the pilot's own diffs).
- **T07 the lead skill and the MCP server move to benchbook-mcp.** Both teach or serve ledger tools, which the hook
  plugin must not load. The skill command becomes `/benchbook-mcp:benchbook-lead`, and the worker's tool names are
  written as `mcp__plugin_benchbook-mcp_benchbook__*` on the assumption that Claude Code keeps a hyphenated plugin
  name in the tool prefix; T08's session check confirms it. bench/real.py loads both plugin directories and counts
  the pre-split server name as its own.
- **T07 init defaults to the code pack only without --metric.** `init --metric X` keeps the core pack, so the
  research quick start and its scripts are unchanged (spec open question 5). Proposals are appended to config.toml
  only when it declares no `[checks]`; off a terminal they are written without asking.
- **T07 uninstall removes the whole state directory.** The spool there holds results not yet recorded; uninstalling
  is the decision to drop them, and the command lists what it removed.
- **T10 "changed since" counts commits that touch the piece's paths** (`git rev-list --count <commit>..HEAD --
  <paths>`), not every commit since: the number says how much of the piece moved, not how busy the branch was.
- **T10 a line with nothing to compare has no mark.** A verified piece with neither task paths nor recorded
  directories, and a git call that times out, leave the line unmarked rather than guess; `commit not in this
  checkout` is shown only when there are paths to compare. A discarded approach whose commit is missing is not marked
  either: its signature still says what failed.
- **T10 "reported, not re-run" means no evidence either way.** A proposed completion with a supporting or refuting
  link is under review, not merely reported, and stays out of the section.
- **T10 the footer keeps the approach hint.** Only the details pointer changed (`benchbook get <id>`); naming the
  approach is what makes a failure a discarded approach, so the brief still asks for it.
- **T09 the 5-second limit covers the whole brief, not only the connection.** The brief runs in a daemon thread
  joined for 5 s, so a slow embedded Postgres start, a slow query or slow git calls all fall back to STATE.md; the
  entry point then ends the process with os._exit rather than wait for the abandoned thread.
- **T09 STATE.md goes in the current checkout** (its git root), not the main worktree's folder that config.toml may
  come from: the brief is scoped by that checkout's branch. It is excluded from git status when the folder is tracked.
- **T09 STATE.md is written even when the brief is empty;** only the injection is skipped.
- **T09 notes move before they are read,** so two hooks at once never deliver one note twice. Other harnesses get
  the empty answer from `context_answer` until T16 reads their documentation.
- **T11 verification deduplication follows the existing spool's database and writer boundary.** Verification
  results are cached there by project, piece and commit. A different authenticated writer has its own processor
  and may run the same piece independently. A reported commit with multiple matching local branches requires an
  explicit piece in the commit or report. The completion report's hash is saved as attempt trace metadata.
- **T11 also re-runs from SubagentStop (T08 run on Claude Code 2.1.330).** Every Agent call came back
  `async_launched`, the one without `run_in_background` too, so the PostToolUse result the document relies on never
  carries a report. SubagentStop does (`agent_type`, `last_assistant_message`), and it is the event with the
  reporter's type, which `check verify` needs for `reported_by`; the task notification's UserPromptSubmit holds the
  same text but no type. SubagentStop gives the lead no context, so the "re-running" line is written as a note
  (`queued-<hash>.txt`, sorted before the result's `verify-<hash>.txt`) and nothing else is delivered there. When
  both events carry the same report (a foreground run), the spool's cached result makes it one re-run.
- **Compaction's SubagentStop is never a report.** Claude Code runs compaction as a subagent whose SubagentStop has
  `agent_type: ""` and the summary as `last_assistant_message`. The capture hook drops it before the wildcard
  settings are read, since the summary repeats the conversation's findings.
- **T13 status groups reports by the latest active commit of each piece.** The queue and last 24 hours of hook
  failures use the same database/writer health checks as doctor. A report overdue for processing also triggers
  the replay warning.
- **T15 live trials take a preregistered local task and oracle manifest.** The dry run exercises the capture
  and scoring pipeline on scripted inputs. Compaction counts only when the SDK stream contains its boundary
  event. The report separates immediate and delayed probes. H5 estimates report tokens from observed Agent
  and Task result payloads.
- **T16 uses the documented context hooks for each harness.** Codex re-injects on SessionStart after compaction.
  Gemini CLI injects at SessionStart and AfterTool because PreCompress cannot modify the summary. OpenCode V2
  uses the first outgoing model-context hook and later continuations for notes. Pi injects the startup brief
  before the first agent turn and adds pending notes to tool results. The two native extensions are installed
  per project.

## After the review of PR #4 (2026-11-23)

- **uninstall removes .benchbook/ only on git's answer.** `git ls-files` that fails (outside a repository, a corrupt
  index) printed nothing, which read as "untracked". The folder is now kept unless git exits 0 with no tracked file.
- **--purge removes a cluster, not a directory.** `BENCHBOOK_DATA_DIR` can name any directory, so purge needs
  `PG_VERSION` there and `pg_ctl status` saying no server runs after the stop; otherwise it keeps the directory and
  says why. `benchbook db stop` now fails when the server is not confirmed stopped.
- **A retried verification finishes from the attempt it recorded.** The spool's receipt is the attempt id. When it is
  already in the ledger, check verify does not run the checks again: it rebuilds the run from the stored metrics
  (statuses, declared checks, guard columns, signature) and writes the link, the discarded approach, the decision
  and the result line from that. A second run could disagree with what is stored and link the opposite way.
- **STATE.md says whose brief it is.** Its first line names the project and the ledger (a hash of the database URL
  without its password, or of the embedded data directory). The fallback is used only for the same project and
  ledger, and only when the ledger could not be reached; a refusal (a revoked token, a failed login, an unknown
  project) gives nothing. A STATE.md from before this change names neither and is not used.
- **Fewer tests are counted per JUnit check.** Sums let tests added to one check hide tests gone from another.
  `tests_before` and `tests_after` stay as sums for the record; `fewer_tests_checks` names the checks that dropped.
- **T03's pass elsewhere must be the implementer's, of the same definition.** The attempt's author is one of the
  piece's reporters, or the Bash hook's `run_by` (the agent type it saw) names one, and `check_definitions` matches
  the merge-base's hash of cmd, junit and timeout. Passes recorded before this change have no definition and no
  longer count: they fall back to refuting, the side that a person can overturn. A `check run` without
  `--agent`/`BENCHBOOK_AGENT` is recorded under the hook's name and does not count either.
- **T03's merge-base run is not cached.** The cache key cannot see the environment, and a failure cached while a
  service was down would turn a real regression into an environment failure after it came back. The test counts
  (T04) still read the cache, whose key now includes the content of the `[verify] copy` files.
- **The boundary benchmark counts a resumed call's own spend.** Claude Code's receipt for a resumed session reports
  the whole session (total_cost_usd and modelUsage); the runner subtracts the session's previous receipt for the
  call's cap, the batch budget and the report, and keeps the session totals beside them.

## PR #4 in use: issues from a real repository (2026-11-23)

Five issues came from running PR #4 head `ec075da` against a scratch clone of a Python repository (hook inputs fed
by hand, not a live session). Four are fixed; the fourth is not.

- **1. The completion gate holds a task only on a live piece's branch.** The gate took every branch other than the
  base as a piece, so where nothing is committed to main every to-do of the lead's own session was held. A branch is
  now a piece when a live `piece start` intent names it (v_inflight) or `BENCHBOOK_PIECE` is set. A config key to
  turn the gate off was the alternative; it adds a setting and leaves the lead blocked by default. The cost: an
  implementer that works on a branch without `piece start` is no longer gated.
- **2. A piece not in HEAD's history is "not merged into this branch".** `git diff` against a commit HEAD never had
  is non-zero without any later change, and rev-list counted 0. `merge-base --is-ancestor` tells the two apart; a
  discarded approach failed at such a commit keeps its place, as for a commit the checkout lacks. A squash-merged
  piece still reads as unmerged once its paths change; telling that apart needs the PR's merge commit in the ledger.
- **3. A report's findings take the piece its commit names** (part 1). The same function as the automatic re-run
  (`verify_requests.reported_target`), so the finding and the verification agree. Part 2, a line in a scoped brief
  counting open findings outside its scope, waits: it changes the brief's format, and the boundary pilot's arm F
  is about to measure brief length and content.
- **5. The automatic re-run is `rerun@<host>`, an orchestrator.** It shared `verifier@<host>` with `check verify`
  without `--agent` (harness), and a project keeps a name's first role, so the run order decided whether a re-run
  could decide. Separate names make both orders behave the same. Rows already written are left alone.
- **4. Not done: caching the merge-base test counts.** Each verify of a piece that adds tests runs the JUnit checks
  again at the merge-base, about 14 s per piece in the measurement, much of it plausibly the new worktree's missing
  `__pycache__` (not measured apart). We keep it: the verdicts are the same, only slower; `ec075da` removed that
  very cache ("Recheck JUnit baselines in the current environment") because its key cannot see the environment, so
  a test skipped by an environment variable makes a cached count disagree with the current one and can report a
  weakening that is not there or miss one that is; a cache would also carry state from one boundary trial into the
  next and blur arm F's verify time and weakening verdicts; and cheaper ways exist that change no verdict (run the
  commit's and the merge-base's worktrees in parallel, or first measure the `__pycache__` share). The pilot's H4
  report states that F's verify time includes the merge-base run.
  **Measured (2026-11-24, `scripts/measure_rerun_queue.py`, model-free).** A synthetic repository (90 tests, one
  JUnit check, tests/** guarded), eight pieces that each add a test file reporting at once, two runs each, WSL2:
  the merge-base run is 47% to 50% of a verify (0.53 s of 1.12 s when the tests only import; 11.4 s of 22.8 s at
  120 ms per test). A fresh worktree's missing `__pycache__` costs 0.08 s of a 0.49 s run (16%) and nothing
  measurable at 120 ms per test, so it is not where the time goes. The last of eight verdicts came after 183 s (9 s
  without the per-test time), and a worker's `check run` queued 14 s in was recorded 168 s after it ran (5.4 s), as
  it waited behind every re-run. Done: the spool's two lanes (the attempt is recorded 0.3 s after it ran; verdicts
  unchanged) and `[verify] parallel_baseline` (verify 11.5 s, last verdict 92 s). Not done: several re-runs at
  once; see "Swarm fan-in".

## Spec §8 verdict for the code pack (2026-11-23)

- **False completion rate 17.9% ≥ 10%: P0 and P1 go ahead.** SWE-bench Verified, 39 instances (40 pre-registered,
  one dropped by the preflight with no reserve left), `claude-sonnet-5-5` on Claude Code 2.1.330: 7 of 39 reported
  completions failed a declared check when re-run (95% Wilson CI 9.0% to 32.7%). **The interval contains 10%**, so
  the sample alone cannot rule out a rate under the threshold; the decision follows the pre-registered point estimate.
  Details: bench/results/2026-11-23-swe-verified/.
- The hidden tests alone refuted 5 of 39 (12.8%, CI 5.6% to 26.7%). Two of the seven refutations come from the
  visible check itself (the reference fix fails it on the same test); without them it is also 5 of 39, so the verdict
  holds either way. The preflight gains a "reference fix passes visible" step for later batches (pre-registration
  amendment of 2026-11-23).
- 4 of 39 completions changed a test file and passed both checks; only 3 of 39 implementers ran any test before
  reporting done, and none of the 7 refuted ones did. Spent USD 1.06 of the 120 cap.

## Swarm fan-in: verdicts to the lead (2026-11-24)

- **Notes go to the lead only.** Notes are found by `session_id`, and Claude Code gives the hooks of a subagent's tool
  calls the lead's `session_id` with the subagent's `agent_id` (hooks reference; every line of a real subagent
  transcript carries the parent's `sessionId`). `bash_result` delivered on every Bash result, a worker's included,
  so with 16 to 30 workers a verdict most likely reached a worker and never the lead. Every delivery point now skips
  an input with an `agent_id` (`session_notes.for_lead`). The boundary pilot's arm F runs 937f5ea, without this; its
  pre-registration amendment of 2026-11-24 says so and counts where verdict lines landed.
- **Every lead tool result can deliver.** The notes hook runs on PostToolUse with matcher `^(?!(Bash|Agent|Task)$)`
  (a JavaScript regex, searched, per the hooks reference), so Bash and Agent results, which deliver from their own
  hooks, do not start a second process. An `async_launched` Agent result delivers too: a lead fanning out calls Agent
  most. Moving a note before reading it keeps any two hooks from delivering it twice (tested with 16 concurrent
  deliveries). PostToolUseFailure of other tools is not wired.
- **No shell fast path.** With no note waiting, `benchbook hook notes` takes p50 35 ms, p95 45 ms (50 runs, Linux
  filesystem, WSL2), under the 100 ms that would have called for one in `bin/benchbook-hook`.
- **The spool's lanes.** Re-runs go to their own lane with its own processor and lock (`.verify.lock`); reports
  and attempts keep `.lock`. Before each re-run the verify lane drains the records lane once under its lock, so
  everything queued before a request is recorded before it runs, as with one processor; an attempt queued while a
  re-run runs may now be in the ledger before that re-run reads the implementers' passes (T03), where before it was
  recorded after. That is the same as a re-run that started a moment later, and a later pass of the same commit is
  the same evidence. Retries, receipts and dead letters are per entry and unchanged; `hook replay` and token
  adoption take both locks, verify lane first, the order the verify lane takes them in. Neither lane strands the
  other's entries (review of PR #6): an attempt written while the verify lane settles finds the records lock held, so
  the verify lane starts the records processor for it afterwards; and each processor starts the other lane's when
  entries wait there with none running, so a re-run left by a stopped processor or an older version is taken up by
  the next hook that starts any processor, as one processor used to.
- **`[verify] parallel_baseline` is opt-in.** The merge-base run is half of a verify that changed tests, and
  running it beside the commit's changes no verdict when the two copies share nothing (the mutation table and the
  test-count tests run both ways). A repository whose checks use a fixed port, a database or a shared directory
  would get failures from the overlap, which would read as the commit's, so the default stays sequential. The early
  run is used only when the commit's run counted exactly the same JUnit checks; its thread's commands can be
  stopped from the main thread (`runner.stop_commands`), so an interrupted verify does not wait for it, and `git
  worktree` add, remove and prune are serialized within the process. SIGTERM and SIGHUP are trapped for the early
  run's whole life, not only while a check runs (run_checks restores the handlers it found): a signal while verify
  waits for it stops the run, removes its worktree and is recorded as the interruption, as a sequential baseline's.
- **Several re-runs at once: not done.** With lanes and the parallel baseline, eight pieces reporting together get
  their last verdict after 92 s and the first after 11 s, and attempts are no longer held up. Pieces finish at
  different times in practice, and concurrent re-runs bring the shared-resource question to every repository, so
  this waits for a swarm run that shows the queue as the bottleneck.
- **Completion gate stays opt-in (B3, pending evidence).** docs/code.md now says the lead runs `piece start` per
  worker. Whether an implementer without one sent a false completion to a merge is read from the boundary pilot's
  arm F and the B1 live check, after the paid measurements; the gate's default is not changed without that.
- **Stop hook: not yet.** Whether a lead ends its turn with a verdict still waiting is to be seen in the live check
  (after the paid measurements end) and arm F's streams; a Stop hook that blocks once is added only if it happens.

## Prompt audit (2026-11-25)

- **Worker replies:** workers hand the lead "a short summary with the record ids" instead of "at most 3 sentences". A
  fixed sentence count is a dated way to set length on current models; what keeps the lead's context small is that
  results live in the ledger, which every worker prompt says. The hard bound that stays is the 1000-character limit on
  a task summary (`complete_task`), enforced in code.
## After the boundary pilot (2026-11-25)

- **A merge waits for its verdict (the merge gate).** Arm F's lead merged all four pieces before their verdicts;
  the docs already said to merge after the verdict line. Of the three options (docs only, a hook that holds the
  merge, a `piece merge` command that waits) the hook was chosen: it acts where the merge happens and asks nothing
  new of the lead. It waits up to 30 s (a re-run took 12-13 s) and holds a merge at most once per commit, so the
  lead and a person stay able to merge; it runs only for `git merge` through the hook's `if`, so the 35 ms start-up
  is not added to every Bash call. A merge the parser misses (`cd x && git merge` in another checkout) is not held.
- **Stop hook: still not done.** No lead in the pilot ended its turn with a verdict waiting: each verdict came as
  the next tool result of the same turn. The merge came first instead, which the gate covers.
- **B3: the completion gate stays opt-in, documented.** Arm F merged four pieces with no false completion
  (false_done 0/4), so there is no case for gating every branch; docs/code.md already tells the lead to run
  `piece start` per worker.
- **A lead records its own dead ends and findings, and is told how at session start.** The pilot's probes asked
  for dead ends and findings that lived only in prompts and the lead's last message; benchbook recorded neither,
  because it records dead ends from failed check runs and findings from worker reports. Reading the lead's own
  messages for them (as `[report]` reads a worker's) was not done: free text would be recorded as findings by
  mistake, and every one costs brief space. Instead two commands write them on purpose, and a code project's
  session start gives a four-line guide even with an empty ledger, since arm F's lead loads no skill and a first
  session gets no brief.

## After the second boundary pilot (2026-11-25)

- **"Changed since" compares files.** `check verify` records the files the commit changed since its merge-base
  (`payload.files`, at most 200; more and only the directories are kept), and the brief compares those with HEAD
  before the piece's declared paths and the directories. The declared paths came first before; files go first now
  because they are what the re-run checked. With `[guard] base` pointing below the integration branch (as in the
  benchmark) a later phase's piece also lists the earlier phases' files, which is coarser than its own changes but
  still finer than directories.
- **A checkout's brief covers the pieces merged into it.** The `--here` scope (the branch's directories and
  `piece:<branch>`) missed records tagged with a merged piece and no file. The brief adds every piece with a
  completion at a commit among HEAD's newest 5,000 (one `git rev-list`, then one query); by commit rather than by
  branch name, since workers' branches are often deleted after the merge. Records of pieces merged long ago come
  back into scope too; the section limits keep the newest first.
