# Rerun of the circle-packing benchmark (2026-11-21)

The first batch (`../2026-11-20-claude-sonnet-5-5/`) run again with the same conditions and with the instrumentation
of `bench/real.py` (saved sessions, per-run usage reconciliation, the ledger export for C), so that later changes
have a baseline to be compared with. Code: commit `d65441a` of the `benchbook` branch (the rename, the core runner,
dedup notify mode, tokens, `check verify`; before `exp run` changed the worker instructions). Claude Code 2.1.330
(the first batch: 2.1.329). 9 runs, USD 9.25 in total.

Files: `report.md` and `metrics.json` (`python -m bench.run`), `cost-breakdown.md`
(`python -m bench.analyze_cost <runs> --compare ../2026-11-20-claude-sonnet-5-5/metrics.json`), `curve.png`.

## Read this before using the C numbers

**Configuration C of this rerun is not a clean baseline.** A and B run with `--strict-mcp-config` and load no MCP
server. C has to keep the plugin's MCP server, so it ran without that flag, and Claude Code also loaded the
claude.ai connectors of the account that ran the benchmark: 90 tools from four connectors next to the plugin's 15. The first batch had the same leak with one connector (8 tools).

| measured, mean per run | first batch | this rerun |
|---|---|---|
| tools in the lead's session, C | 29 | 111 |
| first prompt of the lead, A | 8,999 | 9,014 |
| first prompt of the lead, C | 19,012 | 69,675 |
| lead calls per run, C | 15.0 | 18.3 |
| lead context max, C | 39,861 | 95,777 |
| tokens total (lead), C | 471,683 | 1,586,263 |
| session tokens, C | 1,892,488 | 3,172,897 |
| API cost (USD), C | 1.175 | 1.489 |

The lead reads its first prompt again on every call, so about 50,000 extra tokens of tool definitions times 18 calls
is roughly 0.9 million of C's 3.2 million session tokens (an estimate, not a measurement: 69,675 - 19,012 = 50,663
tokens, 18.3 calls). Subagents were not affected (first prompt of a subagent: C 7,324, A 6,266). A and B are
comparable with the first batch as they are.

`bench/real.py` now sets `ENABLE_CLAUDEAI_MCP_SERVERS=false` for every arm and records the MCP servers each
session started with in `command.json` (commit "Keep the account's claude.ai connectors out of every benchmark
arm"). A clean C baseline needs three more C runs with that fix; they had not been run when this was written.

## What the two batches agree on

Both batches were analysed from their raw streams (`cost-breakdown.md` here and in the first batch's folder).

| hypothesis (plan, section 10) | first batch | this rerun |
|---|---|---|
| 1. lookups and records spend agent turns | partly supported: the calls that follow `check_tried` or `record_experiment` are 29.8% of C's session-token gap over A | contradicted: 18.2% (the gap itself is inflated by the connectors) |
| 2. the exact hash misses near configurations | contradicted: C's repeats are the same config with a new seed (12.3 per run), near configs 0.7 per run against B's 6.0 | contradicted: replicates 10.3 per run, near configs 0.7 against B's 6.3 |
| 3. the brief grows the lead's context | contradicted: the brief is 12.2% of the gap in the lead's largest prompt; the larger first prompt is 95.8% | contradicted: 2.0%; the larger first prompt is 92.9% |

- **Where C's tokens go.** Model calls that follow any benchbook tool call are 77.3% (first batch) and 76.5% (rerun)
  of C's gap over A. The task tools are the largest single category (12.4% and 14.1% of C's prompt tokens), ahead
  of `check_tried` and `record_experiment` together (8.6% and 5.1%).
- **Tokens are not cost.** C used 5.3 times (first batch) and 8.9 times (rerun) A's session tokens, but 2.0 and 2.3
  times its API cost: 89% and 92% of C's tokens are cache reads.
- **Duplicates.** C's duplicate ratio (0.30, 0.24) is between A's (0.41, 0.29) and B's (0.14, 0.16). C has no exact
  repeat (same config, code and seed) in either batch: what the ratio counts in C are replicates with a new seed,
  which the lead's verification asks for.
- **Lost results.** None in A and C; B lost one evaluation in one run of the rerun (0.33 per run).

## What changed in the measurement itself

- All 9 runs reconcile per-message usage with the final totals in every token class, from the saved transcripts
  (the first batch: 0 of 9, because a stream message carries the output count from the start of its response).
- With sessions saved, the SubagentStop hook could check citations: all 12 worker reports per C run were logged as
  `cited` and none was auto-captured (the first batch: all 12 captured). C's ledger therefore has no auto-captured
  notes in this rerun.
- The ledger of C lived in a throwaway embedded Postgres (`BENCHBOOK_DATA_DIR`), not in the machine's own ledger as
  in the first batch.
