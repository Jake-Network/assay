# Changelog

## Unreleased: release readiness (assay 개선.md)

- Build identity: the package version comes from git (`0.1.devN+g<commit>` between tags, `0.1.0` on `v0.1.0`);
  `assay --version`; `assay doctor` prints the build, the database schema revision and this build's newest one.
- `assay doctor` checks the Claude Code connection: `assay` on Claude Code's PATH, plugin `assay@assay` installed
  and enabled, `ASSAY_DATA_DIR`/`ASSAY_PROJECT` in Claude Code settings equal to the shell, recent `hook.log`
  failures and `dead/` reports. Each failure prints the command that fixes it and the exit code is 1.
- Search: every line ends with the record's tags; `--all-tags` (CLI), `all_tags` (SDK, MCP `assay_search`)
  keeps records that have every listed tag (`--tags` keeps those with any).
- Hook: the plugin's `SubagentStop` hook fires for every subagent and `ASSAY_HOOK_AGENTS` (default
  `assay-worker`) picks the captured types; other types exit before reading config or spooling (about 150 ms
  through the console script, which now skips the CLI framework for `assay hook`).
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
