# Benchmark cost breakdown

Written by `python -m bench.analyze_cost`. Every number is labelled. **Measured** numbers come from the API usage in the run's stream or from evals.jsonl. **Estimated** numbers are the chars/4 estimate of block text (`benchbook.tokens.estimate_tokens`). A measured increase of an agent's prompt between two model calls is split between the blocks that entered in between by their share of the estimate. That split is the only estimated part of a measured figure, and the `unsplit` column of section 4 shows how much needed none.

## Runs

| run | model calls (lead + subagents) | session tokens (measured) | lead tokens (measured) | prompt classes reconcile | all classes reconcile | usage source |
|---|---|---|---|---|---|---|
| 30-A-1-try1/A-1 | 4 + 197 | 1,953,456 | 438,564 | yes | yes | transcripts |
| 30-A-2-try2/A-1 | 7 + 268 | 2,571,869 | 549,998 | yes | yes | transcripts |
| 30-C-1-try1/C-1 | 30 + 1529 | 22,096,135 | 2,269,571 | yes | yes | transcripts |
| 30-C-2-try1/C-1 | 20 + 274 | 4,156,147 | 760,007 | yes | yes | transcripts |

4 of 4 runs reconcile per-message usage with the final totals in every token class. 4 of 4 reconcile the three prompt classes, which is what the attribution below uses. Session tokens are the final result event's `modelUsage` (lead and subagents). Lead tokens are its `usage`, which covers the lead's own API calls only and is what `tokens total` in report.md shows. A stream message carries the output count from the start of its response, so output tokens reconcile only from saved transcripts.

## 1. Session totals per configuration

Mean per run. All measured.

| configuration | runs | session tokens | lead prompt tokens | subagent prompt tokens | output tokens | lead calls | subagent calls | subagents | first prompt of the lead | first prompt of a subagent |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 2 | 2,262,662 | 395,483 | 1,611,142 | 256,038 | 5.5 | 232.5 | 87.5 | 8,517 | 5,775 |
| C | 2 | 13,126,141 | 1,470,340 | 11,332,892 | 322,910 | 25.0 | 901.5 | 59.0 | 15,592 | 7,681 |

Token classes of the session, mean per run (measured). Cache reads are billed at a fraction of the other input classes, so the token ratio between configurations is not their cost ratio.

| configuration | input | cache creation | cache read | output | cache read share | API cost (USD) |
|---|---|---|---|---|---|---|
| A | 476 | 444,336 | 1,561,813 | 256,038 | 69.0% | 4.0545 |
| C | 1,853 | 649,082 | 12,152,296 | 322,910 | 92.6% | 2.3058 |

## 2. Prompt tokens by what was read

Mean per run, with the share of the configuration's prompt tokens. A block is counted once for every model call whose prompt contained it. Totals are measured; the split inside one increase is by character share.

| what | A | C |
|---|---|---|
| first prompt of the lead, read by each of its calls | 46,844 (2.3%) | 389,800 (3.0%) |
| first prompt of each subagent, read by each of its calls | 1,341,416 (66.8%) | 6,930,696 (54.1%) |
| benchbook check_tried | 0 (0.0%) | 854,395 (6.7%) |
| benchbook record_experiment | 0 (0.0%) | 841,079 (6.6%) |
| benchbook brief | 0 (0.0%) | 913,757 (7.1%) |
| benchbook claims | 0 (0.0%) | 10,195 (0.1%) |
| benchbook tasks | 0 (0.0%) | 1,228,125 (9.6%) |
| benchbook other | 0 (0.0%) | 60,381 (0.5%) |
| evaluation Bash | 269,501 (13.4%) | 1,020,297 (8.0%) |
| other Bash | 0 (0.0%) | 38,587 (0.3%) |
| file Read | 0 (0.0%) | 9,068 (0.1%) |
| subagent delegation | 80,089 (4.0%) | 89,268 (0.7%) |
| subagent report | 267,447 (13.3%) | 400,618 (3.1%) |
| model text and thinking | 1,329 (0.1%) | 16,964 (0.1%) |
| **all prompt tokens (measured)** | 2,006,625 | 12,803,232 |

## 3. Model calls by what triggered them

Mean per run: model calls, and the prompt tokens those calls were billed (measured). A call is triggered by the tool calls of the response before it; a response with several kinds of tool call splits its follow-up call evenly between them.

| trigger | A | C |
|---|---|---|
| benchbook check_tried | 0.0 calls, 0 (0.0%) | 174.1 calls, 2,208,435 (17.2%) |
| benchbook record_experiment | 0.0 calls, 0 (0.0%) | 224.7 calls, 3,148,860 (24.6%) |
| benchbook brief | 0.0 calls, 0 (0.0%) | 52.8 calls, 522,125 (4.1%) |
| benchbook claims | 0.0 calls, 0 (0.0%) | 4.5 calls, 343,774 (2.7%) |
| benchbook tasks | 0.0 calls, 0 (0.0%) | 124.0 calls, 1,776,428 (13.9%) |
| benchbook other | 0.0 calls, 0 (0.0%) | 5.5 calls, 381,403 (3.0%) |
| evaluation Bash | 145.5 calls, 1,110,206 (55.3%) | 268.8 calls, 3,522,945 (27.5%) |
| other Bash | 0.0 calls, 0 (0.0%) | 6.4 calls, 91,752 (0.7%) |
| file Read | 0.0 calls, 0 (0.0%) | 0.8 calls, 8,365 (0.1%) |
| subagent delegation | 4.0 calls, 382,528 (19.1%) | 5.0 calls, 330,062 (2.6%) |
| start of an agent | 88.5 calls, 513,892 (25.6%) | 60.0 calls, 469,081 (3.7%) |

## 4. Context growth by block category

Mean per run. `estimated` is chars/4 of the blocks, `measured` is their share of the measured prompt increases, `unsplit` is the part of `measured` from increases with a single category.

| configuration | category | blocks | estimated tokens | measured tokens | unsplit | measured / estimated |
|---|---|---|---|---|---|---|
| A | evaluation Bash | 926.0 | 53,748 | 204,476 | 172,558 | 3.80 |
| A | subagent delegation | 90.0 | 16,200 | 32,472 | 0 | 2.00 |
| A | subagent report | 90.0 | 54,642 | 109,606 | 0 | 2.01 |
| A | model text and thinking | 39.5 | 363 | 1,081 | 0 | 2.98 |
| C | benchbook check_tried | 452.0 | 22,388 | 91,404 | 68,550 | 4.08 |
| C | benchbook record_experiment | 562.0 | 37,445 | 136,431 | 108,969 | 3.64 |
| C | benchbook brief | 121.0 | 30,196 | 69,402 | 51,192 | 2.30 |
| C | benchbook claims | 12.0 | 750 | 2,532 | 1,398 | 3.38 |
| C | benchbook tasks | 262.0 | 35,887 | 100,351 | 94,960 | 2.80 |
| C | benchbook other | 19.0 | 2,736 | 6,778 | 4,470 | 2.48 |
| C | evaluation Bash | 639.0 | 43,454 | 134,726 | 101,735 | 3.10 |
| C | other Bash | 18.0 | 381 | 2,371 | 1,775 | 6.22 |
| C | file Read | 2.0 | 302 | 659 | 582 | 2.18 |
| C | subagent delegation | 64.0 | 3,332 | 6,594 | 0 | 1.98 |
| C | subagent report | 64.0 | 14,806 | 29,211 | 0 | 1.97 |
| C | model text and thinking | 192.5 | 668 | 2,023 | 0 | 3.03 |

## 5. The lead's largest prompt

Mean per run: the first prompt plus the measured increases up to the largest prompt, split by character share.

| part | A | C |
|---|---|---|
| first prompt (measured) | 8,517 | 15,592 |
| benchbook brief | 0 | 686 |
| benchbook claims | 0 | 2,279 |
| benchbook tasks | 0 | 28,858 |
| benchbook other | 0 | 6,778 |
| evaluation Bash | 180 | 1,248 |
| other Bash | 0 | 533 |
| subagent delegation | 32,472 | 6,594 |
| subagent report | 109,606 | 29,211 |
| model text and thinking | 26 | 469 |
| **largest prompt (measured)** | 150,802 | 92,248 |

## 6. Repeated and nearby evaluations

From evals.jsonl (measured). Each evaluation is compared with the ones that finished before it in the same run. `near` counts evaluations with no earlier identical config but an earlier one with the same non-numeric fields and every numeric field within the tolerance.

| run | evaluations | duplicate ratio | exact repeat | replicate | near at 10% | near at 25% | near at 50% |
|---|---|---|---|---|---|---|---|
| 30-A-1-try1/A-1 | 444 | 72.5% | 1 | 259 | 2 | 25 | 117 |
| 30-A-2-try2/A-1 | 430 | 78.1% | 1 | 275 | 7 | 50 | 116 |
| 30-C-1-try1/C-1 | 426 | 57.3% | 0 | 180 | 0 | 67 | 185 |
| 30-C-2-try1/C-1 | 133 | 30.8% | 0 | 32 | 0 | 5 | 52 |

## Hypotheses (spec §10)

A verdict is computed from the stated measure: supported at 50% or more, contradicted below 20%, partly supported in between, undetermined when an input it needs is missing.

### Hypothesis 1: Recording and looking up spend agent turns: every attempt costs a separate check_tried and a separate record call.

**Verdict: partly supported** (prompt tokens of the model calls that follow check_tried or record_experiment, as a share of C's session-token gap over A: 49.3%).

- C made 226.0 check_tried and 281.0 record_experiment tool calls for 279.5 evaluations per run (measured). They were followed by 398.8 model calls, 1.3 tool calls per model call: close to one turn for each.
- Model calls triggered by check_tried or record_experiment: 398.8 of 926.5 per run (43.0%), billed 5,357,296 prompt tokens (40.8% of C's session tokens; measured).
- Model calls triggered by any benchbook tool: 585.6 per run (63.2%), billed 8,381,026 prompt tokens (63.8% of C's session tokens; measured).
- C used 10,863,478 more session tokens per run than A (13,126,141 against 2,262,662; measured). The calls that follow a lookup or a record account for 49.3% of that gap, the calls that follow any benchbook tool for 77.1%.
- Model calls per run: C 926.5, A 238.0. First prompt of a subagent: C 7,681, A 5,775 tokens (measured), read again by every one of its calls.

### Hypothesis 2: The exact hash misses nearby configurations; agents that read result files avoid similar configurations by eye.

**Verdict: partly supported** (near configs (within 25%) as a share of C's repeated or nearly repeated evaluations, set to zero when B has at least as many near configs as C: 25.4%).

- C per run: 0.0 exact repeats (same config, code and seed), 106.0 replicates (same config and code, a new seed) and 36.0 evaluations within 25% of an earlier config on every numeric field, out of 279.5 (measured from evals.jsonl).
- Of C's evaluations that repeat or nearly repeat an earlier one, 25.4% are near configs, which an exact hash cannot see; the rest have an identical config, which it does see.
- The benchmark's duplicate ratio (44.1% in C) counts an evaluation when its (config, code) occurs twice, whatever the seed: replicates raise it and near configs never do.
- Near configs per run by tolerance: 10%: A 4.5, C 0.0; 25%: A 37.5, C 36.0; 50%: A 116.5, C 118.5.

### Hypothesis 3: The brief grows the lead's context.

**Verdict: undetermined** (tokens of brief calls and results in the lead's largest prompt, as a share of the gap between C's and A's largest lead prompt: n/a).

- C's largest lead prompt: 92,248 tokens per run (measured), of which 15,592 were there at the first call (system prompt, tool definitions, the lead skill and the task).
- Of the growth after that, brief calls and results account for 686 tokens, all benchbook tool calls and results for 38,601, subagent reports for 29,211 (measured increases, split inside an increase by character share).
- C's largest lead prompt exceeds A's by -58,553 tokens (150,802 in A). The brief accounts for -1.2% of that gap, all benchbook tool calls and results for -65.9%, and the larger first prompt (+7,075 tokens) for -12.1%. The parts need not add up to the gap: A's lead prompt holds 109,606 tokens of subagent reports against C's 29,211.

## What these numbers do not cover

- System prompt, tool definitions, skill text and system reminders are not in the stream. They are inside each agent's first prompt (measured as a whole) and inside the increases, where they are split over the visible blocks or, with none, counted as `not in the stream`.
- Thinking is forwarded without its text, so its tokens are split over the visible blocks of the same increase.
- Output tokens are reported per session and per lead only. Blocks written after an agent's last model call (a subagent's closing report, the lead's structured output) were never part of a prompt and are left out of the tables; they come to A 71,724, C 5,906 estimated tokens per run.

### Injected context seen in the saved transcripts

Lines of type `attachment` by attachment type, summed over the lead and its subagents. `estimated` is chars/4 of the text the transcript records as rendered into the prompt (0 when it records none).

| run | attachment type | lines | estimated tokens |
|---|---|---|---|
| 30-A-1-try1/A-1 | agent_listing_delta | 90 | 35,280 |
| 30-A-1-try1/A-1 | budget_usd | 112 | 3,206 |
| 30-A-1-try1/A-1 | credential_org | 90 | 0 |
| 30-A-1-try1/A-1 | date | 90 | 1,890 |
| 30-A-1-try1/A-1 | deferred_tools_record | 77 | 0 |
| 30-A-1-try1/A-1 | environment | 90 | 19,800 |
| 30-A-1-try1/A-1 | model | 90 | 4,140 |
| 30-A-1-try1/A-1 | prompt_snapshot | 180 | 0 |
| 30-A-1-try1/A-1 | remote_session_change | 90 | 13,680 |
| 30-A-1-try1/A-1 | session_context | 90 | 167 |
| 30-A-1-try1/A-1 | structured_output | 1 | 0 |
| 30-A-2-try2/A-1 | agent_listing_delta | 87 | 34,104 |
| 30-A-2-try2/A-1 | budget_usd | 189 | 5,361 |
| 30-A-2-try2/A-1 | credential_org | 87 | 0 |
| 30-A-2-try2/A-1 | date | 87 | 1,827 |
| 30-A-2-try2/A-1 | deferred_tools_record | 172 | 0 |
| 30-A-2-try2/A-1 | environment | 87 | 19,140 |
| 30-A-2-try2/A-1 | model | 87 | 4,002 |
| 30-A-2-try2/A-1 | prompt_snapshot | 174 | 0 |
| 30-A-2-try2/A-1 | remote_session_change | 87 | 13,224 |
| 30-A-2-try2/A-1 | session_context | 87 | 167 |
| 30-A-2-try2/A-1 | silent_turn_reminder | 1 | 39 |
| 30-A-2-try2/A-1 | structured_output | 1 | 0 |
| 30-C-1-try1/C-1 | agent_listing_delta | 1 | 592 |
| 30-C-1-try1/C-1 | budget_usd | 1469 | 42,664 |
| 30-C-1-try1/C-1 | command_permissions | 1 | 0 |
| 30-C-1-try1/C-1 | credential_org | 91 | 0 |
| 30-C-1-try1/C-1 | date | 91 | 1,911 |
| 30-C-1-try1/C-1 | deferred_tools_record | 450 | 0 |
| 30-C-1-try1/C-1 | environment | 91 | 20,020 |
| 30-C-1-try1/C-1 | mcp_instructions_delta | 91 | 19,838 |
| 30-C-1-try1/C-1 | model | 91 | 4,096 |
| 30-C-1-try1/C-1 | prompt_snapshot | 182 | 0 |
| 30-C-1-try1/C-1 | remote_session_change | 91 | 13,742 |
| 30-C-1-try1/C-1 | session_context | 91 | 167 |
| 30-C-1-try1/C-1 | silent_turn_reminder | 3 | 117 |
| 30-C-1-try1/C-1 | structured_output | 1 | 0 |
| 30-C-1-try1/C-1 | total_tokens_reminder | 1439 | 37,414 |
| 30-C-2-try1/C-1 | agent_listing_delta | 1 | 592 |
| 30-C-2-try1/C-1 | budget_usd | 266 | 7,768 |
| 30-C-2-try1/C-1 | command_permissions | 1 | 0 |
| 30-C-2-try1/C-1 | credential_org | 29 | 0 |
| 30-C-2-try1/C-1 | date | 29 | 609 |
| 30-C-2-try1/C-1 | deferred_tools_record | 98 | 0 |
| 30-C-2-try1/C-1 | environment | 29 | 6,380 |
| 30-C-2-try1/C-1 | mcp_instructions_delta | 29 | 6,322 |
| 30-C-2-try1/C-1 | model | 29 | 1,334 |
| 30-C-2-try1/C-1 | prompt_snapshot | 58 | 0 |
| 30-C-2-try1/C-1 | remote_session_change | 29 | 4,408 |
| 30-C-2-try1/C-1 | session_context | 29 | 167 |
| 30-C-2-try1/C-1 | silent_turn_reminder | 2 | 78 |
| 30-C-2-try1/C-1 | structured_output | 1 | 0 |
