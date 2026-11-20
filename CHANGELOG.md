# Changelog

## Unreleased: review fixes (assay-review.md)

- Core (migration 0006): the rerun oracle compares values against a baseline; TRUNCATE and agent re-roling are
  refused; `assay_sql` cannot call `set_config` and rejects `U&` escapes and `*_to_xml`; seed NULL and -1 differ;
  `v_tried` summarises replicated configs by their mean; candidates no longer need seed 0; `check_tried` reports
  invalidated runs; nested `dedup_ignore` keys.
- Referee: no verification without a verified baseline, repeated calibration rounds, replications by distinct
  agents, replication tasks carry the original command.
- CLI/SDK: no silent `human` agent; `task claim <id>`; `settings.lease_seconds`; experiments link to the
  agent's only leased task; briefs show unjudged findings under the task's tags; worktrees find the main config.

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
