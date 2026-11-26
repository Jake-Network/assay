# Decisions

Each entry states a rule benchbook follows now and why. When a rule changes, its entry is rewritten in place; when the
change followed repeated findings of one kind, the entry keeps one sentence on why the first fix was not enough
(CLAUDE.md, "Review findings"). What changed when, and the reviews and pilots behind it, are in git, CHANGELOG.md and
bench/results/. Pn is a product principle, §n a section of the spec, Tnn a boundary-mode task.

## Positioning

- **A ledger for agent swarms first.** The README and the package summary present benchbook as a Postgres ledger
  for agent swarms: subagents write their results to the database instead of the lead's context, and the lead reads
  them back as a budgeted brief and by SQL, so a swarm can grow without filling the lead's context window. That is
  the problem benchbook was built for. Re-running a reported "done" and replicating experiment results are presented
  as what makes those results safe to build on, not as the headline. From 2026-11-22 to 2026-11-26 the README led
  with verification, because the SWE-bench run supported it (17.9% of reported completions failed a re-run) while
  the H5 fan-in sweep missed its gate; the owner chose the swarm framing on 2026-11-26. The README states H5 as
  measured: report tokens to the lead fell by more than half, peak lead context did not.

## Name

- **benchbook.** The notebook kept next to the bench. `assay` was dropped because four PyPI packages (`assay-ai`,
  `assay-it`, `assay-kit`, `assay-sdk`) install a top-level import `assay`, three install an `assay` command, and they
  describe themselves with the same words (AI evidence, verification layer). `benchbook` was free on PyPI, npm,
  crates.io and as a GitHub account on 2026-11-21. Weak point: it can be read as a benchmark tool, so the README's first
  lines say what it does.
- **Trademark: not searched yet.** The name was chosen from registry availability alone. The search comes before the
  names are reserved (docs/releasing.md, "Before the first release"); its date and result go here.
- **Never move a user's folder on upgrade.** A server or a hook may be using it. A pre-rename location (config folder,
  embedded cluster, state directory) stays in use while the new one has no data, and doctor prints the move.
- **Accept old names everywhere they are persisted; write only new ones.** Blob URIs, transcript markers, the viewer
  cookie, bench keys. The auto-capture hash domain keeps `assay:auto-captured` forever so stored hashes still match.
- **A server started as `assay mcp` keeps the `assay_*` tool names,** which the old plugin's agent definition and
  permission rules name; updating the plugin switches both.
- **Old-name notices never reach stderr on the hook path.** They wait in memory and go to `hook.log`, and only for a
  worker's event.
- **Renaming existing databases is a bootstrap, not a revision.** Legacy databases (0007 at most) are renamed in place
  before Alembic runs, keyed on schema names, so Alembic continues from the database's own revision. Function bodies are
  re-created from the newest definition in the SQL files up to that revision, so they cannot drift from a new database
  (tests assert `pg_dump` equality). Roles are cluster-wide: only the old role, rename it; both, grant the new one and
  revoke the old; only the new one, re-grant. Statements that need an administrator run first; if refused, the whole
  transaction rolls back and the exact SQL is printed. Anything else in the catalog that still names the old schema
  blocks the commit. Row data is never rewritten. `LOCK_KEY` keeps its pre-rename value so old and new binaries
  serialise.

## Package layout

- **Subpackages by function.** `core/` (ledger, models, ids, dedup, tokens, blobs, identity, brief, lookup, runner,
  globs), `checks/` (`check run`, attempts, signatures, verify requests and the stages of `check verify`), `code/`
  (pieces, findings, pr, status), `hooks/` and `cli/`, beside `db/`, `packs/`, `integrations/` and `web/`. At the top
  stay the entry points and what every layer uses (`entry`, `errors`, `config`, `mcp_server`). The runner is core:
  `benchbook ar run` uses it too, and docs/sdk.md offers it.
- **No modules at the old paths.** Before 1.0 the supported API is `from benchbook import Ledger`, the commands, the MCP
  tools and the documented `python -m` entries, which work through `cli/__main__.py` and `hooks/__main__.py`, so a hook
  command line written before an upgrade still runs. Persisted names (pack.toml `brief_compiler`, the pre-rename names)
  do not move. A process of an older version that outlives an upgrade fails its next lazy import: the spool processor
  leaves the entry queued, the MCP server and `benchbook ui` need a restart. Shim modules would avoid the restart at the
  cost of a second set of paths.
- **The `__init__.py` of `checks/` and `hooks/` import nothing.** `hook bash-result` loads modules from both and has 50
  ms, imports included; the import tests name the heavy modules exactly (`benchbook.core.ledger`,
  `benchbook.checks.verify`).
- **A function that tests replace is called through its module** (`rerun.run_checks`, the spooler's functions), so
  replacing it reaches every caller. The Ledger class stays in one file: its methods share one connection and
  transaction discipline.

## Storage and schema

### M0

- **Embedded Postgres:** `pixeltable-pgserver` 0.7.2, PG 16 pinned (`postgres_version=16`; the wheel also ships PG 18).
  pgvector is included; pgcrypto is not needed (`gen_random_uuid()` is core). (P7)
- **Server lifetime:** `cleanup_mode=None`: the first process starts the server and nobody stops it on exit, so one
  Claude Code session ending never cuts off another (`cleanup_mode='stop'` would). `benchbook db stop` stops it
  explicitly and fails when the server is not confirmed stopped.
- **Start-up race:** the package serialises start-up with an inter-process file lock (`fasteners`), so benchbook adds no
  `fcntl.flock` of its own. All embedded-server code stays in `db/conn.py`.
- **Connection pool** per database URL, closed at exit; otherwise every CLI call waits 5 s for pool threads.

### Schema

- **Migrations use Alembic** (the owner's choice; the spec said not to). Each revision runs one plain SQL file, so the
  schema stays reviewable SQL and there is no ORM. Alembic and SQLAlchemy are imported only for an upgrade; the version
  check is plain SQL, which keeps CLI and hook start-up fast. Forward-only. (P7, P10)
- `task_events` has a `project_id` column (not in the spec DDL) so per-project views and export need no join.
- **Project ids and channels.** Ids are slugs (`[a-z0-9][a-z0-9_-]*`, CHECK constraint). The notification channel is
  `benchbook_` + the id up to 53 bytes and a hashed form above that (Postgres limits a channel name to 63 bytes).
- **The unique index counts invalidated runs.** It is what refuses a duplicate, so re-running a config after fixing an
  evaluator needs a different `code_ref` (autoresearch binds the evaluator through the partition instead). Two partial
  indexes, one over non-NULL seeds and one for NULL, replace `coalesce(seed, -1)`; migration 0009 narrows both to rows
  without `duplicate_of`, so notify mode can store repeats.
- **Partitions.** `settings.partition_key` adds a `partition` column to the leaderboard views; without it the expression
  is NULL and every view ranks the whole project.
- **Reader copies are rebuilt.** The per-project copies of the `v_*` views in `benchbook_q` are `SELECT *` snapshots, so
  a migration that changes a view drops and rebuilds all of them.
- **Settings and blobs are append-only;** a settings change notifies. `update_settings` is a JSON merge patch (RFC
  7386), orchestrator or human only, one `settings_events` row each.

### Roles and access

- **`benchbook_sql` isolation.** The copies in `benchbook_q` are filtered by `current_setting('benchbook.project')`.
  `benchbook_reader` is a LOGIN role and `sql()` connects as it, so `set_config('role', ...)` cannot escalate. Migration
  0006 revokes EXECUTE on `set_config` from PUBLIC, database-wide, which suits a database dedicated to benchbook; where
  it cannot, a NOTICE is raised and the keyword and post-query checks remain. The weaker `SET LOCAL ROLE` fallback, for
  a managed Postgres that refuses the reader login, needs `BENCHBOOK_SQL_ALLOW_SET_ROLE=1`. Only SELECT, WITH, TABLE and
  VALUES are accepted; a server cursor bounds the rows, and the 4,000-token cap includes response metadata.
- **TRUNCATE and agent rows.** Statement-level TRUNCATE triggers on every ledger table; agents rows cannot change role,
  id or project, nor be deleted. The owner's privileges (DISABLE TRIGGER) stay open; agents are kept from them by a
  `benchbook_writer` login.
- **Identity under a token (migration 0008).** The session setting carries the token itself and the triggers hash it
  again on each row. A name in a setting is never trusted, and the identity lasts one transaction, so pooled connections
  cannot leak it.
- **Trust boundary.** A session that may update `agents.token_hash` (the owner, a superuser) is trusted; every other
  needs a token. The owner URL is local mode, a `benchbook_writer` login is enforced, and an owner connection can still
  write as anyone.
- **A mismatching author is refused, not rewritten,** so a client sees its forgery attempt fail; a missing author is
  filled from the token.
- **The writer's grants are explicit, table by table.** A migration that adds a table grants it to the writer, and a
  test pins this.
- **Lease cap and `worker_intents` are enforced in `guard_task`; path overlap stays in the SDK,** and so does renewal
  only while live: a database rule for renewal alone would not stop a raw writer from taking the paths.

## Ledger rules

- **Canonical JSON.** Integral floats become ints when hashing, so `1.0` and `1` are the same config in dedup (JSON has
  one number type). Experiments keep the original values that passed the project schema.
- **`is_new_best` and `v_best`.** `is_new_best` is `outcome == 'improved'`, so it honours `improve_epsilon`; `v_best`
  ranks raw values and can show a value that beat the best by less than epsilon.
- **Claim graph** (§6.1 left gaps): `inconclusive` -> supported/refuted/superseded/retracted; `supported` ->
  inconclusive/superseded/retracted as well. `proposed -> verified` needs `force=True` (two decisions otherwise).
  Terminal: refuted, superseded, retracted.
- **Rerun rule** (default `replication`; a pack can choose `all_pass`). `verified` by oracle `rerun` needs valid
  successful runs of the same configuration and code as an original run cited by a supported decision or support link:
  three distinct seeds and two authors by default (`rerun_min_seeds`, `rerun_min_authors`), including an independent
  author. The mean of all evidence runs and the mean of the independent re-runs must both beat a baseline by epsilon,
  the test the autoresearch referee applies. Baseline: `payload.baseline` if numeric, else the best earlier ok result of
  another dedup_key in the original's partition; none means refusal, except for `payload.measurement: true` claims
  (calibration included), which state the evidence mean in `payload.mean` as result claims do. Epsilon is the larger of
  the partition epsilon and `payload.epsilon`, re-read inside the decision transaction. Force applies only to graph
  transitions. (P4)
- **One lock for claims and experiments.** Claim transitions, experiment appends and invalidations share the project
  transaction lock. The latest decision's evidence controls `evidence_invalidated` (support links when it has none).
  Support counts and research activity views exclude invalidated experiments.
- **Identity.** `Ledger.open` has no default agent, so a process that forgot to name itself writes nothing. CLI commands
  that only read fall back to `human`, `brief` included: its `brief_log` row records who read how much, not a claim.
  Commands that write need `--agent`, `BENCHBOOK_AGENT` or, for `decide`, `--as`. Unknown names are registered as
  workers on first use, and a project keeps the role a name was first registered with. (§7.4)
- **Leases.** `settings.lease_seconds` is the live default (900). The renewing UPDATE checks
  `lease_expires_at > clock_timestamp()` itself, so `heartbeat` raises `LeaseLost` once the lease ran out, even if
  nobody took the task. Renewal and occupation share the intent advisory lock, and lease arithmetic under it uses
  `clock_timestamp()`, since `now()` can be older than the lock. `lease_expires_at` tells expiry from release
  (`complete_task` and `cancel_task` clear it), so no new column is needed. A `LeaseLost` renewal without `--paths`
  restores the paths only if the intent ran out; after a release or a takeover it asks for `--paths`.
- **Task linkage.** `record_experiment` without `task_id` links the one non-intent task the agent holds a live lease on
  (with several, the CLI warns). A `complete_task` summary is at most 1,000 characters: details belong in records.
- **Dedup mode is the pack's:** `refuse` (default) or `notify`. In notify mode a repeat is stored with `duplicate_of`,
  which a trigger accepts only when it names the original ok run of the same project, dedup_key and seed. The database
  cannot read pack.toml, so which projects may store repeats is enforced by the SDK only. Every repeat points at the one
  original, counts in `v_tried` (`attempts`, `ok_runs`; not `seeds`), can be `v_best` and is never a `v_partition_best`
  candidate. Its `dedup_hits` row is `notified`, not `rejected`, so "duplicates blocked" counts refusals only.

## Brief

- **Deterministic** given (DB state, task, budget, agent, `now`); `compile_brief(..., now=, log=False)` is what the
  property test uses. Optional items are trimmed bottom-up and long header fields shortened to fit 105% of the budget; a
  request that cannot fit the required structure is a validation error.
- **"Already tried"** with task tags or `spec.config_space` shows only items with relevance > 0 (§8.2); without them,
  most recent first, labelled "total". **"Since your last brief"** lists other agents' records only.
- **The code brief has no `--pack`;** the project's pack chooses the compiler. Tags are a union; `file:` matches by
  prefix; pieces are found through their `piece start` task. Other packs refuse `--tags`.
- **"Reads that changed a plan" is an upper bound:** `brief_log` does not record what a brief showed, so it counts
  citations of claims a brief could have shown.
- **A verified line says whether it still applies (T10).** `applies at HEAD` when the piece's files are the same at HEAD
  (an unmarked line reads as "HEAD moved, so it no longer applies"); `changed since (N commits)` counting only commits
  that touched them, which says how much of the piece moved, not how busy the branch was; `not merged into this branch`
  when HEAD never had the commit (a diff alone would read that as changed); `commit not in this checkout`. The compared
  set is the recorded files (`payload.files`), else the declared paths, else the directories: the files are what the
  re-run checked. Nothing to compare, or a git call past 2 s, leaves the line unmarked rather than guess. A discarded
  approach keeps its place unmarked: its signature still says what failed.
- **A piece's files are its own commits' files.** `check verify` records the files the piece's own commits changed since
  the merge-base (at most 200, else only directories); the merge-base diff alone lists another piece's files when the
  worker branched after that piece was merged. A commit is left out only when something says it is another piece's and
  nothing says it is this one's (another piece's reported commit, a `Benchbook-Piece` trailer naming another piece,
  another piece's check runs at it), and only if another piece's reported commit in the range reaches it. A merge commit
  contributes the files it changed itself (those that differ from every parent). Paths are kept as git has them. With no
  own commit, the merge-base diff is recorded. The errors are not equal: a file listed in excess adds a mark a reader
  can check, a file left out hides a stale verification. The first version left out every commit another piece reaches
  and every merge commit; reviews showed each hiding a stale verification, so a commit is now left out only on positive
  evidence.
- **Re-run states.** "Reported, not re-run" means no evidence either way; a completion with a supporting or refuting
  link is under review. The brief's "Re-run, not decided" is a `proposed` or `inconclusive` completion with any re-run
  linked, supporting or refuting. `status` sorts the same completions by their links: a refutation makes a rerun
  mismatch, supporting re-runs only make "re-run not decided". So a refuted completion nobody decided is in the
  brief's section and counted as a mismatch by `status`. A `supported` completion stays "reported only" until both
  rerun decisions are made.
- **The footer asks for the approach name:** naming it is what makes a failure a discarded approach.
- **A checkout's brief covers the pieces merged into it:** `--here` adds every piece with a completion at a commit among
  HEAD's newest 5,000, by commit rather than branch name, since workers' branches are often deleted after the merge.
- **A lead records its own dead ends and findings on purpose** (`discard`, `finding add`), and a code project's session
  start says how even with an empty ledger. Reading the lead's free text for them would record findings by mistake, and
  each costs brief space.

## Boundary mode

- **The 5-second limit covers the whole brief (T09).** It runs in a daemon thread joined for 5 s, so a slow embedded
  start, query or git call falls back to STATE.md; the process then ends with `os._exit` rather than wait for the
  thread.
- **STATE.md goes in the current checkout's git root,** since the brief is scoped by that checkout's branch, and is
  excluded from git status there. It is written even when the brief is empty. Its first line names the project and the
  ledger (a hash of the URL without password, or of the data directory); it is used only for the same project and
  ledger, and only when the ledger could not be reached, never after a refusal (a revoked token, a failed login, an
  unknown project).
- **status groups reports by each piece's latest active commit (T13)** and checks the queue and the last 24 hours of
  hook failures as doctor does; an overdue report triggers the replay warning.
- **Each harness gets context through its documented hooks (T16);** docs/harnesses.md has which. Gemini CLI injects at
  SessionStart and AfterTool because PreCompress cannot modify the summary.

## Interfaces

- **CLI.** `--json`, `--project`, `--agent` are accepted anywhere on the command line. Usage errors exit 1; 2 is a
  duplicate or a `Conflict` (a live intent holds the key or the paths). `exp run` exits with the command's code, 124 on
  timeout, 128+N on signal, 2 for a duplicate, 1 for a failed write. Extra commands: `get`, `search`,
  `task heartbeat|cancel|show|claim <id>`, `db stop|url`. `python -m benchbook` and `python -m benchbook.cli` dispatch
  through one module, so there is one global state.
- **MCP.** `mcp` 2.x (`MCPServer`), pinned `mcp>=2.3,<3`. Expected failures are raised as `ToolError` so the model sees
  the message (other exceptions are masked). stdio MCP advertises its tools before opening the project, so
  `benchbook init` works in the same session. `benchbook serve` mounts the HTTP MCP at `/mcp` with the SDK's Host-header
  check (DNS-rebinding protection) on.
- **Two Claude Code plugins.** `benchbook` carries hooks only; `benchbook-mcp` carries the MCP server, the
  `benchbook-worker` agent and the lead skill (`/benchbook-mcp:benchbook-lead`), which serve or teach ledger tools that
  a hook-only install must not load. The worker's tools are `mcp__plugin_benchbook-mcp_benchbook__*`: Claude Code keeps
  the hyphenated plugin name in the prefix. Copied by hand, the skill is `/benchbook-lead`. bench/real.py loads both
  plugin directories.
- **Worker prompts.** The worker rules (§12.2) are an English rendering of the spec's six rules. Workers hand the lead
  "a short summary with the record ids": the lead's context stays small because results live in the ledger, not because
  of a sentence count. The hard bound is the 1,000-character task summary, enforced in code.
- **init** defaults to the code pack only without `--metric`, so the research quick start is unchanged. An existing
  config.toml is kept, with only its `project` line rewritten, so `init --pack code` can pin the `[checks]` already
  there; check proposals are appended only when it declares none, without asking off a terminal. A code project's `init`
  names the code commands next. Packs can declare `[project]` defaults; the code pack partitions by piece, which keeps
  the false-completion query within `v_leaderboard`'s 50-per-partition cap.
- **Worktrees.** With no `.benchbook/config.toml` upward, the main worktree's copy is used
  (`git rev-parse --git-common-dir`).
- **uninstall** removes the whole state directory, dropping unrecorded spool entries by decision, and lists what it
  removed. `.benchbook/` goes only when `git ls-files` exits 0 with nothing tracked there: a failing git prints nothing,
  which must not read as "untracked". `--purge` removes a cluster, not any directory: it needs `PG_VERSION` and
  `pg_ctl status` saying no server runs, and otherwise keeps the directory and says why.

## Code pack verification policy

### Declarations and the guard

- **check verify runs the merge-base's declarations,** never the commit's or the verifier's checkout's, so a piece
  cannot choose its own checks; guard and `checks_changed` use the same merge-base. With neither `.benchbook/config.toml`
  nor the pre-rename `.assay/config.toml` at the merge-base, verify uses the newest ledger pin of the piece, else of the project, leaving out pins by the
  piece's implementers, and refuses without one. A config.toml there (at either path) must declare valid `[checks]`: a pin
  does not stand in for an empty or broken table. Default `[guard] paths`: `tests/**`, `**/conftest.py`,
  `.benchbook/config.toml`.
- **Pins (T01).** `checks_source`, `checks_pin` and `checks_sha256` are written only on pinned runs, so other runs keep
  exactly their metrics. The newest pin is in effect; a re-pin counts as a `[checks]` change only when the pin in effect
  at the first attempt is missing or has another sha256, so re-pinning the same declarations sends no piece to a person.
  Only a pin taken from config.toml is compared with the working tree.
- **The guard diff uses `--no-renames`,** so a test moved out of a guarded folder shows as the deletion it is.
- **Added tests are not weakened tests (T04).** A piece usually adds tests, so the guard splits changed guarded files
  into added and weakened (table in docs/code-verify.md, "Added tests and weakened tests"); only a weakening or a
  `[checks]` change sends a piece to a person. `guard_weakened` is written on every attempt. Any change to a
  conftest.py is a weakening: an autouse fixture or a collection hook can weaken every test without deleting any.
- **Names added to an import are lines added.** A Python test file whose deleted lines all sit inside import statements,
  and that still imports every (module, level, name, alias) it did, counts as appended; otherwise adding one name to a
  multi-line import sends a verified piece to a person. Both versions are parsed: a textual rule also passes
  `assert f(1)` rewritten as `assert f(1,` + `2)`. Other languages keep the line rule, and a file that does not parse is
  weakened (tests/test_mutations.py, `IMPORT_ROWS`).
- **Fewer tests are counted per JUnit check,** since sums let tests added to one check hide tests gone from another
  (`fewer_tests_checks`).

### Recording and deciding

- **Recording.** The completion claim is proposed in `--reported-by`'s name (one per piece and commit); the attempt is
  by the verifier (`verifier@<host>`, harness role, by default), which must differ from the reporter; a supports/refutes
  link joins them. An interrupted run is recorded as error with no link. Diff text and check output are never stored; of
  `--report-file` only sha256, size and the check names it mentions.
- **A claim's identity is (piece, commit, reporter).** `statement_hash` is client-supplied and unique per project only,
  so it cannot carry identity; on a clash verify writes a statement naming the reporter, as a last resort a random hash.
  A server-side hash check would refuse honest callers and not stop pre-emption.
- **Under a token the verifier records the claim,** because a token is one agent; the reporter goes in
  `payload.reported_by`.
- **verify writes supported, then verified, after a dry run,** keeping the state graph instead of using `force`.
- **The verify rule is the pack's** (`[verify] rule`: `replication` or `all_pass`). `min_authors` counts the implementer
  and must be at least 2, so an implementer never verifies alone.
- **Who the implementer is.** The claim's author and `payload.reported_by` (the author is left out only when a lead,
  person or harness names another reporter). Every completion claim of a piece and commit adds its reporter,
  `reported_by` is trusted only on claims a lead, person or harness recorded, and sets never shrink, so a worker cannot
  bar the lead or CI from deciding by naming them. The implementer's attempts never count, and it cannot decide
  `verified` on its own completion. In local mode one process can still use two names; tokens on a writer login stop
  that.
- **An attempt says what was declared** (`checks_declared`). Only complete attempts are evidence under all_pass and in
  the PR summary; one without the field is partial, since a missing field must not read as "everything". A failed check
  in a partial run still blocks; a partial pass is not linked and does not make a full failure "disagree".
- **Integrity flags and failures are sticky per commit.** Any valid finished attempt at the commit that failed, weakened
  tests or changed `[checks]` keeps the rerun oracle out, cited or not; invalidating it is the way back.
- **Disagreement is inconclusive in either direction; a lone failing re-run decides nothing,** because `refuted` is
  final and flakes exist.
- **A retried verification finishes from the attempt it recorded.** The spool's receipt is the attempt id; when it is
  already in the ledger, verify rebuilds the run from the stored metrics instead of running again, since a second run
  could disagree with what is stored and link the opposite way.
- **A nameless re-run that could decide says how.** A nameless run only records (CI depends on that). For
  `verifier@<host>` the line ends with the same `check verify` run with `--agent lead`, pinned to the same claim,
  commit, base and project, but only when it would decide verified: every declared check passed, no run of the commit
  failed, and the claim is proposed or supported. After weakened tests or when the lead is an implementer it names
  `decide --as human`; after a failed or partial run, nothing, since the first version pointed to re-runs that could not
  decide. A re-run rather than `decide --oracle rerun`, which takes two commands. No hint under a token, which names one
  agent.
- **The credential scrub in `check verify` is hygiene, not isolation:** code under test still runs as the verifier's
  user.
- **The runner waits for the group** after a timeout or an interruption (signal 0, reaping the shell whose zombie keeps
  the group alive), then SIGKILLs it after the grace time.

### Environment failures (T03)

- **An environment failure needs a pass elsewhere.** A goal check fails at the merge-base by design, so "fails the same
  way at the merge-base" alone would never refute a commit that leaves it failing. The implementer's own clean run of
  the commit must have passed it: passing there and failing in the clean worktree as at the merge-base is the
  environment's signature. The merge-base run happens only then, and a commit equal to its merge-base is not compared.
- **That pass must be the implementer's, of the same definition:** its author is a reporter of the piece (or the Bash
  hook's `run_by` names one), and `check_definitions` matches the merge-base's. A pass without a definition, or a
  `check run` without `--agent`, does not count, so the failure refutes, the side a person can overturn.
- **"The same way" includes subsets:** a JUnit check whose failing tests all fail at the merge-base too.
- **No merge-base result is cached,** for environment failures or test counts. A cache key cannot see the environment: a
  failure cached while a service was down would excuse a real regression later, and an environment-dependent skip would
  make a cached count report a weakening that is not there or miss one. Swarm fan-in has what speeds the merge-base run
  up instead.

### The automatic re-run

- **It decides as `rerun@<host>`, an orchestrator.** A project keeps a name's first role, so sharing `verifier@<host>`
  (harness) with a nameless `check verify` would let the run order decide whether a re-run can decide.
- **It starts from PostToolUse on Agent/Task and from SubagentStop (T11).** Claude Code returns Agent calls as
  `async_launched`, so their PostToolUse may carry no report; SubagentStop carries it with the reporter's `agent_type`,
  which `check verify` needs for `reported_by`. SubagentStop gives the lead no context, so its "re-running" line is a
  note. When both events carry one report, the spool's cached result makes it one re-run.
- **Deduplication follows the spool's database and writer boundary:** results are cached there by project, piece and
  commit, and another authenticated writer may re-run the same piece. A commit on several matching local branches needs
  an explicit piece. The report's hash is kept in the attempt's trace.
- **It retries a failure once.** A failure outside the piece (a timing-sensitive test) costs the lead a held merge and a
  dead end in the brief. A failure the retry does not repeat is still not a pass: the runs disagree, so the retry
  decides inconclusive, and verified needs a lead to invalidate the failed run first. What changes is that a failure
  seen once leaves no discarded approach. `check verify` by hand does not retry. docs/code.md, "What `check verify`
  decides", has the cases.
- **The failed run is recorded before its retry runs,** under an id derived from the request's receipt, and the retry
  under the receipt itself, naming the failed run, so every later try finds where it stopped. Recording both after the
  retry lost the refutation on a crash between the writes and lost which run had failed on a later try, so the merge
  gate gave the wrong advice.

### JUnit evidence

- **`junit = true` means "a test ran".** Exit 0 with no report, no testcase or only skipped ones is a failed check;
  `check run` keeps the command's exit code.
- **A JUnit pass counts only from a report path of its own.** `check run` and `check verify` give each invocation its
  own report path and validate it. The Bash hook records a raw JUnit check's failure or timeout and observes a raw pass
  as `unrecordable`, and the completion gate ignores raw Bash passes of JUnit checks. Three fixes tried to tell whether
  a shared report was written by this command (a time window, an mtime after the start with a fingerprint, a PreToolUse
  before-and-after snapshot); each let through a report written by something else, because a shared path cannot name its
  writer.

### Signatures and discarded approaches

- **One signature module:** JUnit, then tsc, then exit code plus normalised last error line, then timeout. Several
  failures add a count and a set hash, so different sets never merge. The fallback is heuristic (lines that differ only
  in numbers merge), so JUnit is recommended. Signatures sharing a `check:part` element on a piece are "seen before", a
  notice only.
- **check run looks up only after a failure,** in a 2 s thread with one plain connection, and never starts the embedded
  server, so a passing run pays nothing.
- **Discarded approaches.** One per (piece, signature, approach) under the fixed domain `benchbook:discarded\0`; later
  failures add evidence, runs with uncommitted changes leave none, and the claim survives verification. Writing it never
  raises, so the spool never records an attempt twice.

### Pieces and findings

- **A piece is a task:** an intent tagged `piece` and `piece:<name>` with spec `{piece, paths, declared}`, so it needs
  no new table, status, queue or migration.
- **Paths are expanded once, at start;** a glob matching none or more than 200 files is stored as the glob. The overlap
  test is exact (it returns a witness path; an unreadable pattern overlaps) and runs inside the intent advisory lock, so
  of concurrent starts one wins. The lease is 7200 s; the holder's restart renews it (`--paths` replaces the paths),
  `piece done` by the holder ends the task and by a lead cancels it, expiry frees the paths, and `check verify` does not
  release.
- **Findings use existing claim kinds and statuses:** an `observation` tagged `out-of-scope`; accepted is `supported`
  with the issue as a cited source, rejected is `refuted`; the report is the evidence, so `n_support` counts reports.
  `finding accept` exists because no other command creates a source.
- **The finding dedup key is file + normalised sentence,** under the fixed domain `benchbook:out-of-scope\0`, without
  the author, so reports from different subagents or tokens merge and the stored hash never changes between releases.
- **Findings are parsed in the background processor,** not the hook process, which gains no work or imports; the heading
  is stored with the queued report so a replay reads the run's setting. A `mod:` tag appears only when the item writes
  it.
- **A report's findings take the piece its commit names,** through the function the automatic re-run uses
  (`verify_requests.reported_target`), so the two agree.

### The implementer's own runs

- **check run records through the spool, never inline.** Its 300 ms budget leaves no room for the ledger imports, and a
  database outage must not touch the exit code. The gate counts passing runs still in the spool.
- **Implementer runs are marked.** `metrics.via` (`check-run`, `bash-hook`) marks a run in the implementer's own
  checkout, never an independent re-run; all_pass skips such runs as passes and lets their failures block.
  `worktree_dirty` marks a run that did not test `code_ref`; the gate ignores it.

## Hooks

- **Hook input (Claude Code hooks reference).** `SubagentStop` carries `last_assistant_message`; a `SubagentHandback`
  report is in `tool_input.message` of its `PostToolUse`. The plugin hooks both; the handler also reads `PostToolUse` on
  `Agent` (`tool_response` text blocks).
- **Uncited output is captured:** subagent output that cites no existing `e:/c:/t:` id. A hash of the full report gives
  idempotence while keeping distinct reports with the same opening.
- **Compaction's SubagentStop is never a report.** It has `agent_type: ""` and the summary as `last_assistant_message`,
  which repeats the conversation's findings; the capture hook drops it before reading the wildcard settings.
- **The spool.** Entries are written atomically under `~/.local/state/benchbook/spool/` by database target; a detached
  processor retries them, and later hooks or `benchbook hook replay` resume after a longer outage.
- **The spool is keyed by writer.** An entry's author is decided at processing time from the token, so each token has
  its own spool directory, and the writer digest in each entry makes a misplaced entry wait instead of being recorded
  under the wrong name. The token is never written.
- **Rotation is bridged by what the ledger said earlier.** A rotated token cannot authenticate, so whose an old spool is
  comes from its `writer.info` (target and writer from the first entry, the agent once a ledger confirms it). A replay
  counts queues it cannot attribute as `held` and fails. Like the entries, the note guards against mix-ups between
  agents sharing a state directory, not against one of them editing files.
- **Adoption is idempotent.** A copy's name derives from the original's, and it is written only when neither it nor its
  record in `adopted.done` exists. An entry keeps its receipt through rotations; for a check attempt that is its
  experiment id, so a retry after the database committed reuses the row.
- **Hook matching is word equality** after dropping quotes, leading assignments and redirections, with one leading
  `cd DIR &&` and optional `{junit}`, from the repo root. Pipelines and lists are observed, not recorded (their exit
  code is not the check's); `check run` is recorded once, by the wrapper.
- **The completion gate fails open** on no project, no checks, no piece, no commit, a project outside the code pack, or
  any error.
- **The completion gate holds a task only on a live piece's branch** (a live `piece start` intent names it, or
  `BENCHBOOK_PIECE` is set). Treating every non-base branch as a piece held every to-do of a lead's own session; a
  config switch would add a setting and leave the lead blocked by default. The cost: a branch without `piece start` is
  not gated, so docs/code.md tells the lead to run it per worker. Gating every branch waits for a false completion seen
  reaching a merge without it.
- **Whether SubagentStop can block** is open question 1 (docs/claude-code.md); the plugin gates on TaskCompleted only.

### Other harnesses

- **The harness is named in the hook command (`--harness`), not guessed:** Codex and Claude Code inputs look alike.
- **No exit code, no record.** A run whose outcome the input does not give (Codex `exec_command`, a Cursor
  `postToolUseFailure` error, a wrapped Gemini `llmContent`) is `unrecordable`, never a pass; `check run` stays the
  canonical path. Fixtures are built from the harness documentation.
- **No completion gate outside Claude Code:** Codex and Cursor document no task-completed event, and their SubagentStop
  "block" and `followup_message` only continue the agent.
- **Foreign transcripts are not read;** Codex and Cursor reports are captured as notes. Doctor compares input shape, not
  version numbers (only Cursor's input has one).

## Swarm fan-in

- **Notes go to the lead only.** Claude Code gives the hooks of a subagent's tool calls the lead's `session_id` plus the
  subagent's `agent_id`, so with many workers a verdict delivered on any Bash result most likely reaches a worker. Every
  delivery point skips input with an `agent_id` (`session_notes.for_lead`).
- **Every lead tool result can deliver.** The notes hook's matcher `^(?!(Bash|Agent|Task)$)` leaves out Bash and Agent
  results, which deliver from their own hooks; an `async_launched` Agent result delivers too, since a lead fanning out
  calls Agent most. Notes move before they are read, so no two hooks deliver one twice. PostToolUseFailure of other
  tools is not wired. With no note waiting the hook takes p50 35 ms, under the 100 ms that would call for a shell fast
  path.
- **A merge waits for its verdict (the merge gate).** Leads merge a piece before its verdict even when the docs say to
  wait. A hook acts where the merge happens and asks nothing new of the lead, unlike a `piece merge` command. It waits
  up to 30 s and holds a merge at most once per commit, so the lead and a person can still merge, and it runs only for
  `git merge` (the manifest's `if`). A merge the parser misses (`cd x && git merge` in another checkout) is not held.
- **The spool's lanes.** Re-runs have their own lane, processor and lock (`.verify.lock`), so a worker's attempt is
  recorded at once instead of waiting behind every re-run. The verify lane drains the records lane once before each
  re-run, so everything queued before a request is recorded before it runs. `hook replay` and adoption take both locks,
  verify lane first. Neither lane strands the other's entries: each processor starts the other lane's when entries wait
  there with none running.
- **`[verify] parallel_baseline` is opt-in.** The merge-base run is about half of a verify that changed tests, and
  running it beside the commit's changes no verdict when the two copies share nothing. Checks that use a fixed port, a
  database or a shared directory would fail from the overlap in a way that reads as the commit's, so the default is
  sequential. The early run is used only when both runs counted the same JUnit checks, and an interruption stops it and
  removes its worktree.
- **Measured** (`scripts/measure_rerun_queue.py`, eight pieces at once, a check suite of about 11 s): the first
  verdict came after 23 s and the last after 183 s, or 11 s and 92 s with `[verify] parallel_baseline`; for a suite
  that waits more than it computes one verify went from 22.8 s to 11.5 s. The merge-base run is 47% to 50% of a verify
  that needs one, and a fresh worktree's missing `__pycache__` is not where the time goes.

## CI and the GitHub Action

- **CI re-runs rather than reading other workflows' results.** The action runs `check verify` itself, so a CI attempt
  has the same shape and rule as a lead's.
- **With a shared ledger, CI never reports a completion.** It links to an existing claim (`--reported-by` its reporter);
  with none, it records only the attempt and the piece shows "not reported", as does a pull request rebased after the
  lead's verify. `pr pieces --claims` passes the claim id to verify, which checks its project, piece, commit and
  reporter.
- **The summary's "verified" is the rule, not the decision.** It applies the ledger's rule check to the recorded runs
  and shows the claim's status beside it; precedence is verified, then disagrees, then claimed only.
- **CI identity.** Owner URL: self-declared `ci:<run_id>`, a worker. Token: the token's agent for every run, with the
  run in `trace_ref`. The job that holds the credential runs the pull request's code, so a harness token is recommended
  and CI passes `--no-decide` unless asked and holding a lead token.
- **Only the configured author's marked comment counts,** and extra copies are deleted. Forks and Dependabot pass with a
  notice (they get no secrets); a missing secret on a same-repository pull request fails. With a shared ledger, a pull
  request with no pieces gets no new comment.
- **The action installs benchbook from its own tree** (`0.0.0+action`), so the commands match the action.
- **Without `database-url` the action keeps a ledger of its own run.** An independent re-run that marks weakened tests
  is worth having from CI alone, without a shared Postgres, a secret and commit trailers. It re-runs the head as one
  piece `pr-<number>`, reported by its author, whenever no trailer names the head commit. Each invocation has its own
  ledger folder, and a run with nothing to re-run updates an earlier comment rather than leave its old verdict standing.
  The checks come only from the config committed at the merge-base: a proposal from the pull request's files or a ledger
  pin would let the pull request choose its own checks. For the same reason init runs in an empty repository under
  `RUNNER_TEMP`, not the checkout, and the `BENCHBOOK_*` and `ASSAY_*` ledger variables are unset. Deciding and tokens
  need a shared ledger and are ignored with a warning.

## Experiments and autoresearch

- **exp run.** Refuse mode blocks a same-key, same-seed earlier run before it starts: ok runs (invalidated included)
  always, failed-only runs unless `--retry-failed`; notify mode runs it and reports `duplicate_of`. The command's output
  is unchanged; exp run adds one `benchbook: ` line (or JSON with `--json`) and puts notes on stderr. Metrics are read
  only from existing formats (`parse_summary` lines, a last-line JSON object whose `value` is the primary metric and
  whose `seed` is not a metric); the command gets `BENCHBOOK_EXP_CONFIG` and `BENCHBOOK_EXP_SEED`.
- **Partition-aware outcome.** The epsilon is `settings.partitions[p].epsilon`, else `improve_epsilon`, re-read inside
  the writer's lock because calibration runs in another process. `outcome_baseline = "verified"` (set by `ar init`)
  compares with the partition's best verified mean, and with the raw best before anything is verified.
- **Intents** are tasks inserted as `claimed` by their creator (`max_attempts` 1, tag `intent`).
  `create_intent(..., exclusive=[field])` refuses a live intent with the same `spec[field]`, under an advisory lock;
  `ar` uses `partition|knob changes` for claims and `partition|code hash` for runs.
- **next_task** takes `spec_match` (jsonb containment), so replication tasks go only to agents in the same partition,
  and `spec.distinct_agents_for`, so an agent never takes two replications of one candidate. Claims by one agent in a
  project are serialized.
- **ml-research pack.** `ok_metrics_required = ["val_bpb"]` is checked in code (a JSON Schema cannot see run_status).
  Packs can name a `brief_compiler`; the ml-research brief replaces the default one.
- **Verified needs both means (T5, stricter than §10.2).** The mean of all runs and the mean of the replications alone
  must beat the verified best by epsilon: the first run was selected for being good, so it must not count twice.
- **Calibration doubles as the baseline's replication.** The first code of a partition is re-run twice; the referee
  stores sigma/epsilon and verifies that code without a comparison, so the partition gets its first verified best. Fewer
  than 3 successful runs store `status: partial|failed` and the floor. A calibration not `verified` starts another round
  with fresh seeds (at most `calibration_rounds`, default 3); claims, tasks and verdicts are keyed by (origin, round).
- **No auto-verify without a baseline.** A candidate whose replications finish while its partition has no verified
  result is left unjudged.
- **Distinct replicators.** A verdict whose replications share an author or include the original author is
  `inconclusive`.
- **`replicate_max_queue` counts candidates** (default 4). A better arrival cancels the unclaimed tasks of the weakest
  candidate; otherwise it is closed `inconclusive` without replication. Candidates beaten by a newer verified best are
  closed, and a candidate whose first run no longer beats "new best + epsilon" loses its unclaimed tasks and ends
  `inconclusive`.
- **Candidates** are the first ok run of their config, whatever the seed; the referee replicates seed-0 runs, which is
  what `ar run` records.
- **Result claims** are proposed by the referee with the first run as evidence, decided `supported` (oracle metric) and
  then `verified` (oracle rerun, all runs). Statements include the experiment's short id, so they never collide. A
  rerun-rule rejection becomes `inconclusive`.
- **ar run refuses failed code too** (a crash usually repeats); `--retry-failed` overrides. A record that cannot be
  written is queued in `.benchbook/ar_pending.jsonl` for the next `ar` command, so every started run is recorded through
  a database blip.
- **Parent code** is compared by normalised hash, so a comment-only commit still counts as committed; a re-run of the
  parent records no parent.
- **Hardware class.** Vendor words, memory types and "gpu" are dropped; form factors (pcie, sxm4, laptop) are kept
  because they change speed. Memory comes from the name, else `memory.total` rounded to GB.
- **Imports are first runs only.** `ar import` reads code with `git show <commit>:train.py`; an imported record has
  replication number 0 and is imported once per hardware class, whoever imports it. Improvements become candidates and
  get replicated: no log can stand in for an independent re-run.
- **The evaluator is bound through the partition, not `code_ref`.** `code_ref` stays the `train.py` hash so live re-runs
  can match imported originals; the `prepare.py` hash names the partition (`trace_ref.evaluator`).
- **A measured run without recorded code is `invalid`:** kept as a record that the approach was tried, never a candidate
  or a baseline.
- **`ar gate on` adds no mechanism.** It restores the gate settings where missing or weaker, and leaves stricter values
  and an explicit `time_budget_s = 0` alone.
- **program.benchbook.md** keeps the §9 loop block verbatim (Korean comments, as the spec requires) with an English
  rendering; the rest is upstream `program.md` adapted to the shared loop, with MIT attribution in an HTML comment
  (upstream's README states MIT).
- **Simulator.** Noise is drawn fresh per run, runs sleep instead of training, and they print `sim_run_id`, `true_bpb`
  and `lucky` so the e2e can check that nothing started went unrecorded; the e2e's lucky rate (0.15) makes a 1-minute
  run exercise refutation.

## Bench

- **Circle-packing evaluator.** Violations up to 1e-9 are accepted, but every radius is reduced by the largest violation
  before summing, so the tolerance never adds score; anything larger is invalid.
- **Deadlines use `time.monotonic()`,** since the WSL wall clock steps by tens of seconds. The demo's rerun claims
  report the observed minimum for the listed seeds.
- **Dry and real runs.** `bench --dry` simulates agents with stated assumptions (drop rate 0.15, compaction at 3,000
  tokens, 400 tokens per evaluation step); its report, like `--sim` numbers (dominated by CLI start-up), is labelled a
  pipeline check, not evidence. Real mode prints the `claude -p` commands unless `--execute` is passed (CLAUDE.md, "Paid
  runs"). Real runs share an API dollar cap and use separate project ids; evaluation receipts tie each result across
  lead reports, files and the ledger. Token curves use reconciled usage checkpoints or a measured endpoint.
- **Arm A of the autoresearch bench** is collected by a `uv` shim on PATH (wrap_train.sh); its "false best" is a keep
  whose 3-run re-run mean is worse than its reported value by more than B's calibrated epsilon.
- **Token accounting.** The benchmark keeps sessions, so the SubagentStop hook can verify citations. Output counts in
  the stream are taken at the start of each response, so only transcripts reconcile all four token classes. "tokens
  total" is the lead's own `result.usage`; whole-session totals (`modelUsage`) are in `cost-breakdown.md`. A resumed
  call's own spend is its receipt minus the session's previous one, for its cap, the batch budget and the report.
- **Arms exclude account connectors.** Configuration C needs the plugin's MCP server, so it cannot use
  `--strict-mcp-config`; `ENABLE_CLAUDEAI_MCP_SERVERS=false` is set for every arm. The baseline for comparisons is the
  batch at `d65441a` with C's runs without connectors.
- **Cost verdicts are computed.** A cost hypothesis is supported when its mechanism explains at least 50% of the gap it
  targets and contradicted below 20%; near configs use a 25% relative tolerance per numeric key. C costs about 1.8 times
  A, from model calls that each re-read their context, not from near configurations, so no near-config notice is
  planned.
- **SWE-bench task set.** Selection reads the harness's own copy of Verified (`SWE-bench/SWE-bench_Verified`, which
  swebench 5 evaluates from) and checks princeton-nlp's ids, labels and base commits against it. The visible check is
  the hidden eval script without the test patch, on the PASS_TO_PASS tests that exist before it (`instances.json`); a
  preflight confirms it, including that the reference fix passes it, and replaces failures from same-repo reserves.
- **A harness error is not a false completion.** `swe_check` exits 3, the attempt is invalidated, and the batch stops
  until the harness is fixed. "Reported done" that cannot be verified (missing commit, history that does not descend
  from the declarations) is listed beside the rate.
- **Phase 1 verdict for the code pack (spec §8): go ahead.** False completion rate 17.9% (7 of 39, 95% CI 9.0% to 32.7%)
  against a 10% threshold; the interval contains 10%, so the decision follows the pre-registered point estimate.
  bench/results/2026-11-23-swe-verified/ and bench/results/2026-11-21-phase1/report.md.
- **Boundary trials (T15)** take a preregistered local task and oracle manifest. Compaction counts only when the SDK
  stream contains its boundary event; immediate and delayed probes are reported apart. H5 estimates report tokens from
  observed Agent and Task result payloads.

### Pilot tools

- **`--force-compact` compacts at a fixed point.** The boundary brief is meant for compaction, and a pilot lead's
  context stays far below the smallest window (100,000 tokens). `--compact-pct` makes automatic compaction earlier, but
  the arms carry different contexts, so one percentage would compact them at different points. `--force-compact` sends
  `/compact` at the same point of every phase in every arm, and its `trigger: manual` boundary event is the sample the
  registration requires. Both exist because only a paid smoke run shows whether headless `/compact` compacts; the owner
  chooses. The `/compact` call runs with the probe's cap and turn limit, and one that emits no manual `compact_boundary`
  stops the run. A phase's own automatic compactions are kept as `auto_compactions`. The host's `DISABLE_AUTO_COMPACT`
  and `DISABLE_COMPACT` are removed from a run's environment, and boundaries count only the call's own session and the
  expected trigger.
- **The `discarded` probe asks for the identifier only;** "and why?" drew sentences that contain the right signature and
  fail the exact match. Counting an answer that contains the identifier (as a whole token: `control-1` is not in
  `control-12`) is a sensitivity column, not the registered measure, so the measure does not follow the results.

## Known limits and not done

- **Not prevented:** a fake JUnit report written by the code under test; a raw writer taking a piece name or paths up to
  the lease cap; self-declared names in local mode; background processes a command leaves behind after a normal exit
  (the runner kills the group only after a timeout or interruption).
- **Autoresearch (Should):** the LLM research-director mode, the `--trust candidate` ablation, `ar init --shim`,
  hands-on MLX/macOS fork checks. `sync --trust candidate` itself exists.
- **Several re-runs at once.** Pieces finish at different times in practice, and concurrent re-runs bring the
  shared-resource question to every repository; this waits for a swarm run that shows the queue as the bottleneck.
- **A Stop hook that holds a lead's turn while a verdict waits,** added only if a lead is seen ending its turn with one
  waiting; so far each verdict came as a next tool result of the same turn.
- **A scoped brief line counting open findings outside its scope.** It changes the brief's format.
- **A squash-merged piece** reads as unmerged once its paths change; telling it apart needs the PR's merge commit in the
  ledger.
- **The GitHub Action on a real runner** has not been confirmed.

## Former sections

Until 2026-11-26 this file was a log by date. Where the current entries of each old section are now:

| Former section | Now in |
|---|---|
| M0 spike (2026-11-19) | Storage and schema, "M0"; Hooks; Interfaces |
| Schema and SDK; Review fixes (2026-11) | Storage and schema; Ledger rules; Experiments and autoresearch |
| Brief; Interfaces | Brief; Interfaces; Hooks |
| Demo and bench; Spec §8 verdict for the code pack (2026-11-23) | Bench |
| Step 3: autoresearch (2026-11-20) | Experiments and autoresearch; Known limits |
| Name (2026-11-21); Package layout (2026-11-26) | Name; Package layout |
| Core for the verification ledger (2026-11-21) | Ledger rules; Storage and schema; Experiments and autoresearch; Bench |
| Code pack; Plan execution (2026-11-21) | Code pack verification policy; Hooks; CI and the GitHub Action; Brief; Bench |
| After the independent, second, third and fourth review (2026-11-21) | Code pack verification policy, "JUnit evidence" and others; Hooks; Ledger rules; Known limits |
| Boundary mode, T01-T16 (2026-11-22) | T01, T04: "Declarations and the guard"; T03: "Environment failures"; T11: "The automatic re-run"; T07: Interfaces; T09, T13, T16: Boundary mode; T10: Brief; T15: Bench |
| After the review of PR #4; PR #4 in use (2026-11-23) | Code pack verification policy (item 4: "Environment failures"); Hooks (item 1); Brief (item 2); Interfaces; Boundary mode |
| Swarm fan-in: verdicts to the lead (2026-11-24); After the boundary pilot (2026-11-25) | Swarm fan-in; Hooks; Brief; Known limits |
| Prompt audit (2026-11-25) | Interfaces, "Worker prompts" |
| After the second and third boundary pilot (2026-11-25) | Brief; "The automatic re-run" |
| First-run fixes, the action without a ledger, pilot tools (2026-11-26) | Code pack verification policy; Ledger rules; Brief; CI and the GitHub Action; Bench, "Pilot tools" |
