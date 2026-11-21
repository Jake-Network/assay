# Changelog

## Unreleased: code pack

- `benchbook check verify <piece> <commit> [--reported-by NAME] [--report-file F] [--base REF] [--checks a,b]`
  re-runs the checks declared in `.benchbook/config.toml` `[checks]` on a reported commit in a clean worktree. It
  records the completion claim (the implementer's) and the attempt (the verifier's), linked supports/refutes, and
  notes guarded files and `[checks]` changes. See docs/code.md.
- New `code` pack and `benchbook init --pack`. `--pack code` defaults to metric `checks_passed` (max),
  partitioned by piece.
- `.benchbook/config.toml` accepts `[checks.<name>]` (cmd, junit, timeout) and `[guard]` (paths, base).

## Unreleased: core for the verification ledger

- `benchbook exp run --config '<json>' [--seed N] -- <cmd>` checks the ledger, runs the command (output shown
  unchanged) and records the result in one command. Duplicates in a refuse-mode pack are not run (exit 2);
  `--retry-failed` re-runs a config that only failed. Metrics come from `key: number` lines or a JSON last line.
  Non-zero exits, timeouts and interrupts are recorded; the exit code passes through.
- Worker instructions (plugin, manual copy, skill, AGENTS.md) use `exp run` where a shell is available; the MCP
  tools remain the fallback. Plugin 0.1.2. The capture hook recognises `exp run` receipts.
- circle-packing: `run_config.py` scores one `packing.py` config; README (b) uses `exp run`.
- `bench`: real runs save the Claude Code session, copy the lead's and subagents' transcripts into the run
  directory, and record per-run usage reconciliation (`usage_check`) in `metrics.json`. Configuration C also
  keeps the queued hook entries and a `benchbook export` of its project.
- `bench`: new `python -m bench.analyze_cost <runs>` writes `cost-breakdown.md`: prompt tokens by what entered
  the context, repeated and near evaluations, and a verdict for each cost hypothesis.
- Core (migration 0009): `dedup_mode = "refuse" | "notify"` in pack.toml. `refuse` (default; core and ml-research)
  is the behaviour so far. With `notify`, `record_experiment` stores an ok run whose config, code_ref and seed
  were already recorded and returns the first such run as `duplicate_of` with a `notice`; the MCP tool returns
  both, `benchbook exp record` exits 0 (notice in `--json`, on stderr otherwise). `experiments.duplicate_of`
  marks the repeat, `dedup_hits` logs it as `notified`, and the unique indexes still refuse any duplicate that
  does not declare its original.
- Hook: a recorded repeat (new id with `duplicate_of`) counts as the subagent's own write.
- Per-agent tokens: `benchbook agent add NAME --role worker` prints a token once (only its SHA-256 is stored);
  `--rotate`, `agent revoke` and `agent list` manage it. Clients read `BENCHBOOK_TOKEN`.
- Migration 0008: the `benchbook_writer` login. On it, every write must carry the token of the agent it names,
  and decisions, settings, open tasks and cancellations need an orchestrator or human token. Owner connections
  (local mode) are unchanged. `export()` no longer includes agent token hashes.
- Runner: the command runner is core (`benchbook.runner`). `run_command` takes `metric=None` (exit code 0 alone
  is ok) and `tee=True` (the output goes to the log file and, unchanged and as it arrives, to stdout). The
  autoresearch runner module re-exports the same names.
- `benchbook ar import --from pi-autoresearch|uditgoenka|codex-autoresearch <path> --hw <class>` records the run
  logs of the three autoresearch skills as experiments. Imported keeps become candidates that other agents
  re-run. Importing the same log again adds nothing.
- `benchbook ar gate on [--from <skill>]` turns on the reproduction gate (verified baseline, referee
  replications, rerun rule) in one command, creating the project if needed, and writes a program fragment for
  the skill. Imported runs, results.tsv included, now get the time-budget check on the training time the log
  reports.

## Unreleased: renamed to benchbook

- The project is now `benchbook`: distribution, import package, command, `BENCHBOOK_*` environment variables,
  `.benchbook/config.toml`, database schema `benchbook` / `benchbook_q`, role `benchbook_reader`, MCP tools
  `benchbook_*`, plugin `benchbook@benchbook`, agent `benchbook-worker`, skills `benchbook` and
  `benchbook-lead`, `program.benchbook.md`. Why: DECISIONS.md, "Name".
- The `assay` command still works during 0.1.x: it prints one notice and runs `benchbook`. Deployed
  `assay hook ...` hooks stay silent and write the notice to `hook.log`.
- `ASSAY_*` variables are read when the `BENCHBOOK_*` one is unset, with one notice per process.
- `.assay/config.toml`, the old embedded cluster (`~/.local/share/assay/pg`, or
  `~/Library/Application Support/assay/pg` on macOS) with its `assay` database, and `~/.local/state/assay` stay
  in use while no new one exists. `benchbook doctor` shows how to move each one; nothing is moved for you.
- Records with `assay://blob/` artifacts, reports queued by the old build, and the old plugin (`assay@assay`,
  `assay-worker`, `assay_*` tools) keep working.
- A database made before the rename (schema `assay`, role `assay_reader`) is renamed in place by
  `benchbook migrate` or the first connection, in one transaction. Functions and reader views are rebuilt, and
  stored `search_path` settings are rewritten. Old rows keep their `assay://blob/...` URIs. `benchbook doctor`
  reports a pre-rename schema; `benchbook migrate --json` includes `renamed_from`. A database holding both the
  `assay` and `benchbook` schemas is refused, and the message says which one to drop. When the owner may not
  rename or create roles, nothing changes and the message prints the SQL for an administrator.
- Project ids longer than 53 bytes (was 57) use the hashed notification channel name.
- Release: `.github/workflows/release.yml` publishes to PyPI from a `v*` tag with Trusted Publishing; CI builds the
  wheel and runs `uvx --from dist/*.whl benchbook doctor` and `benchbook init` on empty Linux and macOS runners
  (`scripts/smoke_wheel.sh`). The checklist is docs/releasing.md.

## Unreleased: release readiness

- Build identity: the package version comes from git (`0.1.devN+g<commit>` between tags, `0.1.0` on `v0.1.0`);
  `assay --version`; `assay doctor` prints the build, the database schema revision and this build's newest one.
- `assay doctor` checks the Claude Code connection: `assay` on Claude Code's PATH, plugin `assay@assay` installed
  and enabled, `ASSAY_DATA_DIR`/`ASSAY_PROJECT` in Claude Code settings equal to the shell, recent `hook.log`
  failures and `dead/` reports. Each failure prints the command that fixes it and the exit code is 1.
- Search: every line ends with the record's tags; `--all-tags` (CLI), `all_tags` (SDK, MCP `assay_search`)
  keeps records that have every listed tag (`--tags` keeps those with any).
- Hook: the plugin's `SubagentStop` hook fires for every subagent and `ASSAY_HOOK_AGENTS` (default
  `assay-worker`) picks the captured types; other types exit before reading config or spooling (about 150 ms
  through the console script, which now skips the CLI framework for `assay hook`). Plugin version 0.1.1, so
  existing installs pick up the new hook with `claude plugin update assay@assay`; `assay doctor` flags an
  install that still has the old matcher.
- `assay doctor` also compares `ASSAY_DATABASE_URL` between the shell and Claude Code (by server and database, never
  printed), reads hook failures per database spool (a replay of another database's spool no longer hides them,
  and reports queued for over 5 minutes count), and keeps going when one check raises (a bad `~user` path).
- Hook replay: a malformed entry from an earlier build (`tool_input` not an object) no longer stops the
  entries after it.
- README: what assay fits (a number per attempt, re-runnable, one space explored together) and why delegating
  code implementation is not one of them.
- Benchmark: subagents run in the foreground (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`); newer Claude Code started
  them in the background and the lead ended before its workers reported.

## Unreleased: review fixes (assay-review.md)

- Core (migration 0006): the rerun oracle compares values against a baseline; TRUNCATE and agent re-roling are
  refused; `assay_sql` cannot call `set_config` and rejects `U&` escapes and `*_to_xml`; seed NULL and -1 differ;
  `v_tried` summarises replicated configs by their mean; candidates no longer need seed 0; `check_tried` reports
  invalidated runs; nested `dedup_ignore` keys.
- Referee: no verification without a verified baseline, repeated calibration rounds, replications by distinct
  agents, replication tasks carry the original command.
- CLI/SDK: no silent `human` agent; `task claim <id>`; `settings.lease_seconds`; experiments link to the
  agent's only leased task; briefs show unjudged findings under the task's tags; worktrees find the main config.
- Core (migration 0007): rerun result claims bind to their evidence, independent reruns determine reproducibility,
  and concurrent claims by one agent respect replication groups. Briefs tolerate legacy partition epsilon values.
- Viewer and MCP: non-loopback HTTP binds require a token, duplicate checks report invalidated seeds,
  and task claims and heartbeats use the current project lease default.
- Hooks: confirm cited record IDs against write receipts and continue replaying after malformed entries or
  concurrent arrivals.
- Autoresearch: keep completed runs across database outages, resolve known legacy partition hashes, recover
  from an invalidated calibration, and decide local KEEP independently of shared replication eligibility.
- Autoresearch benchmark: carry forward each agent's remaining API budget, reconcile collector and ledger
  run receipts, and validate provenance before aggregating repetitions.

## Unreleased: Step 3, autoresearch on many GPUs

- Core: partitions (`settings.partition_key`), per-partition epsilon, verified-best baseline, `update_settings`
  with an audit table, content-addressed blobs, worker intents with exclusive keys, `next_task` exclusions and
  spec matching, views `v_partition_best`, `v_lineage`, `v_inflight`, `v_knob_history` (migration 0005).
- `ml-research` pack: result and calibration claims, config and metrics schemas, its own brief.
- `assay ar`: init, brief, claim, run, replicate, sync, note, idea, status, import, referee; deterministic
  referee (calibration, replication, verdicts); `program.assay.md`; `launch_local.sh`; `assay blob put/get`.
- Viewer: partition selector, verified-best staircase with candidates and refuted runs, in-flight work,
  lineage and knob pages, train.py diffs against the parent.
- CPU simulator with scripted agents and an e2e swarm test; launch experiment harness (`bench/autoresearch`).
- `python -m assay`; `python -m assay.cli` no longer ignores global options in sub-commands.

## 0.1.0.dev0 (unreleased)

- Bound brief and SQL output, keep invalidated experiments out of briefs, and flag claims whose evidence was invalidated.
- Check rerun evidence against the original experiment, serialize decisions, and preserve exact validated configs.
- Preserve distinct hook reports, retry pending captures, and let MCP start before project initialization.
- Isolate real benchmark runs, reconcile evaluator receipts, and measure comparisons under a shared budget.
- Cover long project IDs, escape viewer errors, and enforce install QA on supported Linux and macOS runners.

- M0: embedded Postgres (pixeltable-pgserver, PG 16) with a server that outlives its starter; MCP 2.x and Claude
  Code plugin/hook formats checked; results in DECISIONS.md.
- M1: schema (Alembic revisions running plain SQL), append-only and decision-role triggers, NOTIFY, 10 read views,
  `assay_reader` role; `assay.Ledger` SDK; invariant tests I1-I7 (incl. 32 workers x 500 tasks).
- M2: `assay` CLI with `--json` and exit codes 0/1/2/3.
- M3: deterministic, budgeted brief compiler; property test over 1,000 random ledgers.
- M4: MCP server (stdio + streamable HTTP), worker and orchestrator tool sets, `assay_sql` safety, `assay://schema`.
- M5: Claude Code plugin (MCP, `assay-worker` subagent, `assay` and `assay-lead` skills, result-capture hook), manual
  install files, AGENTS.md snippet.
- M6: read-only viewer (overview with best-so-far curve, experiments, claims board, tasks, record pages; live via SSE).
- M7: circle-packing demo with an LLM-free scripted swarm.
- M8: benchmark harness (A baseline / B files / C assay), `--dry` mode and report generation.
- M9: README (en/ko), concepts, schema, Claude Code, multi-machine and SDK docs.
