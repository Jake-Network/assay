# Changelog

## 0.1.0.dev0 (unreleased)

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
