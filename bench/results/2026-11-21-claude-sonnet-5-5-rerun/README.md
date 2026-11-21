# Baseline of the circle-packing benchmark (2026-11-21)

The first batch (`../2026-11-20-claude-sonnet-5-5/`) run again with the same conditions and with the instrumentation
of `bench/real.py` (saved sessions, per-run usage reconciliation, the ledger export for C). Later changes are
compared with this batch. Code: commit `d65441a` of the `benchbook` branch (the rename, the core runner, dedup
notify mode, tokens, `check verify`; before `exp run` changed the worker instructions). Claude Code 2.1.330 (the
first batch: 2.1.329). 12 runs in all, USD 12.65.

| file | what |
|---|---|
| `report.md`, `metrics.json`, `curve.png` | the baseline: A and B from the nine-run batch, C from three runs with the account's connectors switched off |
| `cost-breakdown.md` | where the tokens went, and the verdicts on the plan's three cost hypotheses (`python -m bench.analyze_cost`) |
| `with-connectors/` | the nine-run batch as it ran, C included, kept because it shows what the leak costs |

## Why C was run twice

A and B run with `--strict-mcp-config` and load no MCP server. C has to keep the plugin's MCP server, so it ran
without that flag, and Claude Code also loaded the claude.ai connectors of the account that ran the benchmark: 90
tools from four connectors next to the plugin's 15. `bench/real.py` now sets
`ENABLE_CLAUDEAI_MCP_SERVERS=false` for every arm and records the MCP servers of each session in `command.json`;
the three C runs of the baseline have that one change and loaded the plugin's server only. The first batch had the
same leak with one connector (8 tools), so its C numbers are slightly high too.

| C, mean per run (measured) | first batch (8 connector tools) | with connectors (90) | baseline (0) |
|---|---|---|---|
| tools in the lead's session | 29 | 111 | 21 |
| first prompt of the lead | 19,012 | 69,675 | 15,913 |
| lead calls | 15.0 | 18.3 | 12.7 |
| lead context max | 39,861 | 95,777 | 33,058 |
| tokens total (lead) | 471,683 | 1,586,263 | 326,104 |
| session tokens | 1,892,488 | 3,172,897 | 1,499,439 |
| API cost (USD) | 1.175 | 1.489 | 1.132 |

## The baseline

| mean per run | A text | B files | C benchbook |
|---|---|---|---|
| evaluations | 60 | 58.3 | 59.3 |
| lost results | 0 | 0.33 (one evaluation in one run) | 0 |
| duplicate ratio | 29.4% ± 15 | 16.1% ± 5.4 | 22.5% ± 10 |
| lead context max (tokens) | 30,499 | 35,577 | 33,058 |
| tokens total (lead) | 94,066 | 190,528 | 326,104 |
| session tokens (lead and subagents) | 356,554 | 1,137,757 | 1,499,439 |
| API cost (USD) | 0.635 | 0.960 | 1.132 |
| final best | 2.636 | 2.636 | 2.636 |

- C uses 4.2 times A's session tokens and 1.8 times its cost (87% of C's tokens are cache reads); B uses 3.2 times
  and 1.5 times. C's lead uses 3.5 times the tokens of A's lead.
- C's largest lead prompt is 8% above A's (33.1k against 30.5k) and below B's.
- With three runs per arm the duplicate ratios overlap within one standard deviation; C has 0.7 exact repeats per
  run (same config, code and seed), the rest of its ratio are replicates with a new seed.

## The three cost hypotheses

| hypothesis (plan, section 10) | first batch | baseline |
|---|---|---|
| 1. lookups and records spend agent turns | partly supported: the calls that follow `check_tried` or `record_experiment` are 29.8% of C's session-token gap over A | partly supported: 30.3% |
| 2. the exact hash misses near configurations | contradicted: near configs 0.7 per run in C against 6.0 in B | contradicted: 1.3 against 6.3 |
| 3. the brief grows the lead's context | contradicted: 12.2% of the gap in the lead's largest prompt | partly supported by share (29.8%), of a gap of only 2,559 tokens: the brief is 762 tokens of C's largest prompt |

- Model calls that follow any benchbook tool call are 77.3% (first batch) and 77.9% (baseline) of C's gap over A.
  In the baseline, C's prompt tokens by what was read: the task tools 8.5%, the brief 6.3%, `check_tried` 5.7%,
  `record_experiment` 4.3%; the first prompts (lead and subagents, read again on every call) 64.5%.
- C makes 111.7 model calls per run against A's 36.0: the number of calls, each re-reading its agent's context, is
  what the gap is made of, more than the size of any one tool result.

## What changed in the measurement itself

- All 12 runs reconcile per-message usage with the final totals in every token class, from the saved transcripts
  (the first batch: 0 of 9, because a stream message carries the output count from the start of its response).
- With sessions saved, the SubagentStop hook could check citations: all 12 worker reports per C run were logged as
  `cited` and none was auto-captured (the first batch: all 12 captured), so C's ledger has no auto-captured notes.
- The ledger of C lived in a throwaway embedded Postgres (`BENCHBOOK_DATA_DIR`), not in the machine's own ledger as
  in the first batch.
