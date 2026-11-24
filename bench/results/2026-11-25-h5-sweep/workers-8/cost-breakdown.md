# Benchmark cost breakdown

Written by `python -m bench.analyze_cost`. Every number is labelled. **Measured** numbers come from the API usage in the run's stream or from evals.jsonl. **Estimated** numbers are the chars/4 estimate of block text (`benchbook.tokens.estimate_tokens`). A measured increase of an agent's prompt between two model calls is split between the blocks that entered in between by their share of the estimate. That split is the only estimated part of a measured figure, and the `unsplit` column of section 4 shows how much needed none.

## Runs

| run | model calls (lead + subagents) | session tokens (measured) | lead tokens (measured) | prompt classes reconcile | all classes reconcile | usage source |
|---|---|---|---|---|---|---|
| workers-8/A-1 | 4 + 68 | 645,397 | 135,163 | yes | yes | transcripts |
| workers-8/A-2 | 4 + 58 | 580,704 | 139,879 | yes | yes | transcripts |
| workers-8/A-3 | 4 + 62 | 619,517 | 148,474 | yes | yes | transcripts |
| workers-8/C-1 | 19 + 230 | 3,568,405 | 717,333 | yes | yes | transcripts |
| workers-8/C-2 | 16 + 205 | 3,021,969 | 575,680 | yes | yes | transcripts |
| h5-sweep-c8r/C-1 | 18 + 226 | 3,483,630 | 722,982 | yes | yes | transcripts |

6 of 6 runs reconcile per-message usage with the final totals in every token class. 6 of 6 reconcile the three prompt classes, which is what the attribution below uses. Session tokens are the final result event's `modelUsage` (lead and subagents). Lead tokens are its `usage`, which covers the lead's own API calls only and is what `tokens total` in report.md shows. A stream message carries the output count from the start of its response, so output tokens reconcile only from saved transcripts.

## 1. Session totals per configuration

Mean per run. All measured.

| configuration | runs | session tokens | lead prompt tokens | subagent prompt tokens | output tokens | lead calls | subagent calls | subagents | first prompt of the lead | first prompt of a subagent |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 615,206 | 111,587 | 432,530 | 71,089 | 4.0 | 62.7 | 24.7 | 8,524 | 5,811 |
| C | 3 | 3,358,001 | 650,746 | 2,585,790 | 121,466 | 17.7 | 220.3 | 24.0 | 10,768 | 7,668 |

Token classes of the session, mean per run (measured). Cache reads are billed at a fraction of the other input classes, so the token ratio between configurations is not their cost ratio.

| configuration | input | cache creation | cache read | output | cache read share | API cost (USD) |
|---|---|---|---|---|---|---|
| A | 133 | 125,855 | 418,129 | 71,089 | 68.0% | 1.1334 |
| C | 476 | 259,684 | 2,976,376 | 121,466 | 88.6% | 2.2402 |

## 2. Prompt tokens by what was read

Mean per run, with the share of the configuration's prompt tokens. A block is counted once for every model call whose prompt contained it. Totals are measured; the split inside one increase is by character share.

| what | A | C |
|---|---|---|
| first prompt of the lead, read by each of its calls | 34,097 (6.3%) | 190,233 (5.9%) |
| first prompt of each subagent, read by each of its calls | 363,976 (66.9%) | 1,689,441 (52.2%) |
| benchbook check_tried | 0 (0.0%) | 145,113 (4.5%) |
| benchbook record_experiment | 0 (0.0%) | 157,959 (4.9%) |
| benchbook brief | 0 (0.0%) | 210,279 (6.5%) |
| benchbook claims | 0 (0.0%) | 10,148 (0.3%) |
| benchbook tasks | 0 (0.0%) | 316,745 (9.8%) |
| benchbook other | 0 (0.0%) | 28,487 (0.9%) |
| evaluation Bash | 68,248 (12.5%) | 236,653 (7.3%) |
| other Bash | 0 (0.0%) | 93,808 (2.9%) |
| subagent delegation | 20,487 (3.8%) | 20,109 (0.6%) |
| subagent report | 56,748 (10.4%) | 120,098 (3.7%) |
| model text and thinking | 561 (0.1%) | 4,714 (0.1%) |
| other tool | 0 (0.0%) | 12,747 (0.4%) |
| **all prompt tokens (measured)** | 544,117 | 3,236,535 |

## 3. Model calls by what triggered them

Mean per run: model calls, and the prompt tokens those calls were billed (measured). A call is triggered by the tool calls of the response before it; a response with several kinds of tool call splits its follow-up call evenly between them.

| trigger | A | C |
|---|---|---|
| benchbook check_tried | 0.0 calls, 0 (0.0%) | 29.7 calls, 348,890 (10.8%) |
| benchbook record_experiment | 0.0 calls, 0 (0.0%) | 36.6 calls, 523,600 (16.2%) |
| benchbook brief | 0.0 calls, 0 (0.0%) | 13.8 calls, 152,506 (4.7%) |
| benchbook claims | 0.0 calls, 0 (0.0%) | 3.3 calls, 149,451 (4.6%) |
| benchbook tasks | 0.0 calls, 0 (0.0%) | 51.2 calls, 725,275 (22.4%) |
| benchbook other | 0.0 calls, 0 (0.0%) | 3.6 calls, 149,526 (4.6%) |
| evaluation Bash | 38.0 calls, 289,171 (53.1%) | 65.9 calls, 805,443 (24.9%) |
| other Bash | 0.0 calls, 0 (0.0%) | 5.8 calls, 62,562 (1.9%) |
| subagent delegation | 3.0 calls, 103,063 (18.9%) | 3.0 calls, 121,667 (3.8%) |
| other tool | 0.0 calls, 0 (0.0%) | 0.2 calls, 2,823 (0.1%) |
| start of an agent | 25.7 calls, 151,883 (27.9%) | 25.0 calls, 194,792 (6.0%) |

## 4. Context growth by block category

Mean per run. `estimated` is chars/4 of the blocks, `measured` is their share of the measured prompt increases, `unsplit` is the part of `measured` from increases with a single category.

| configuration | category | blocks | estimated tokens | measured tokens | unsplit | measured / estimated |
|---|---|---|---|---|---|---|
| A | evaluation Bash | 248.7 | 15,291 | 56,397 | 41,514 | 3.69 |
| A | subagent delegation | 24.7 | 5,137 | 10,181 | 0 | 1.98 |
| A | subagent report | 24.7 | 14,271 | 28,312 | 0 | 1.98 |
| A | model text and thinking | 14.7 | 175 | 441 | 0 | 2.51 |
| C | benchbook check_tried | 172.7 | 7,638 | 27,284 | 8,742 | 3.57 |
| C | benchbook record_experiment | 236.7 | 15,242 | 54,851 | 26,871 | 3.60 |
| C | benchbook brief | 52.0 | 11,846 | 29,054 | 2,441 | 2.45 |
| C | benchbook claims | 8.0 | 479 | 1,752 | 938 | 3.66 |
| C | benchbook tasks | 114.0 | 16,275 | 43,044 | 38,368 | 2.64 |
| C | benchbook other | 15.3 | 1,218 | 3,465 | 1,362 | 2.84 |
| C | evaluation Bash | 260.0 | 17,806 | 50,771 | 15,923 | 2.85 |
| C | other Bash | 22.0 | 461 | 6,066 | 4,022 | 13.15 |
| C | subagent delegation | 24.0 | 1,167 | 2,242 | 0 | 1.92 |
| C | subagent report | 24.0 | 6,625 | 12,738 | 0 | 1.92 |
| C | model text and thinking | 72.3 | 339 | 1,027 | 0 | 3.03 |
| C | other tool | 0.7 | 29 | 750 | 0 | 26.16 |

## 5. The lead's largest prompt

Mean per run: the first prompt plus the measured increases up to the largest prompt, split by character share.

| part | A | C |
|---|---|---|
| first prompt (measured) | 8,524 | 10,768 |
| benchbook brief | 0 | 1,678 |
| benchbook claims | 0 | 1,752 |
| benchbook tasks | 0 | 14,464 |
| benchbook other | 0 | 3,465 |
| evaluation Bash | 0 | 1,745 |
| other Bash | 0 | 5,321 |
| subagent delegation | 10,181 | 2,242 |
| subagent report | 28,312 | 12,738 |
| model text and thinking | 141 | 250 |
| other tool | 0 | 750 |
| **largest prompt (measured)** | 47,159 | 55,172 |

## 6. Repeated and nearby evaluations

From evals.jsonl (measured). Each evaluation is compared with the ones that finished before it in the same run. `near` counts evaluations with no earlier identical config but an earlier one with the same non-numeric fields and every numeric field within the tolerance.

| run | evaluations | duplicate ratio | exact repeat | replicate | near at 10% | near at 25% | near at 50% |
|---|---|---|---|---|---|---|---|
| workers-8/A-1 | 120 | 42.5% | 0 | 40 | 3 | 14 | 58 |
| workers-8/A-2 | 121 | 30.6% | 3 | 26 | 4 | 12 | 65 |
| workers-8/A-3 | 131 | 33.6% | 5 | 27 | 3 | 19 | 60 |
| workers-8/C-1 | 115 | 32.2% | 0 | 26 | 3 | 6 | 36 |
| workers-8/C-2 | 115 | 46.1% | 1 | 35 | 0 | 0 | 24 |
| h5-sweep-c8r/C-1 | 125 | 33.6% | 0 | 27 | 0 | 6 | 40 |

## Hypotheses (spec §10)

A verdict is computed from the stated measure: supported at 50% or more, contradicted below 20%, partly supported in between, undetermined when an input it needs is missing.

### Hypothesis 1: Recording and looking up spend agent turns: every attempt costs a separate check_tried and a separate record call.

**Verdict: partly supported** (prompt tokens of the model calls that follow check_tried or record_experiment, as a share of C's session-token gap over A: 31.8%).

- C made 86.3 check_tried and 118.3 record_experiment tool calls for 118.3 evaluations per run (measured). They were followed by 66.3 model calls, 3.1 tool calls per model call: the agents batch several lookups or records into one turn.
- Model calls triggered by check_tried or record_experiment: 66.3 of 238.0 per run (27.9%), billed 872,490 prompt tokens (26.0% of C's session tokens; measured).
- Model calls triggered by any benchbook tool: 138.2 per run (58.1%), billed 2,049,249 prompt tokens (61.0% of C's session tokens; measured).
- C used 2,742,795 more session tokens per run than A (3,358,001 against 615,206; measured). The calls that follow a lookup or a record account for 31.8% of that gap, the calls that follow any benchbook tool for 74.7%.
- Model calls per run: C 238.0, A 66.7. First prompt of a subagent: C 7,668, A 5,811 tokens (measured), read again by every one of its calls.

### Hypothesis 2: The exact hash misses nearby configurations; agents that read result files avoid similar configurations by eye.

**Verdict: contradicted** (near configs (within 25%) as a share of C's repeated or nearly repeated evaluations, set to zero when B has at least as many near configs as C: 11.9%).

- C per run: 0.3 exact repeats (same config, code and seed), 29.3 replicates (same config and code, a new seed) and 4.0 evaluations within 25% of an earlier config on every numeric field, out of 118.3 (measured from evals.jsonl).
- Of C's evaluations that repeat or nearly repeat an earlier one, 11.9% are near configs, which an exact hash cannot see; the rest have an identical config, which it does see.
- The benchmark's duplicate ratio (37.3% in C) counts an evaluation when its (config, code) occurs twice, whatever the seed: replicates raise it and near configs never do.
- Near configs per run by tolerance: 10%: A 3.3, C 1.0; 25%: A 15.0, C 4.0; 50%: A 61.0, C 33.3.

### Hypothesis 3: The brief grows the lead's context.

**Verdict: partly supported** (tokens of brief calls and results in the lead's largest prompt, as a share of the gap between C's and A's largest lead prompt: 20.9%).

- C's largest lead prompt: 55,172 tokens per run (measured), of which 10,768 were there at the first call (system prompt, tool definitions, the lead skill and the task).
- Of the growth after that, brief calls and results account for 1,678 tokens, all benchbook tool calls and results for 21,360, subagent reports for 12,738 (measured increases, split inside an increase by character share).
- C's largest lead prompt exceeds A's by 8,013 tokens (47,159 in A). The brief accounts for 20.9% of that gap, all benchbook tool calls and results for 266.6%, and the larger first prompt (+2,243 tokens) for 28.0%. The parts need not add up to the gap: A's lead prompt holds 28,312 tokens of subagent reports against C's 12,738.

## What these numbers do not cover

- System prompt, tool definitions, skill text and system reminders are not in the stream. They are inside each agent's first prompt (measured as a whole) and inside the increases, where they are split over the visible blocks or, with none, counted as `not in the stream`.
- Thinking is forwarded without its text, so its tokens are split over the visible blocks of the same increase.
- Output tokens are reported per session and per lead only. Blocks written after an agent's last model call (a subagent's closing report, the lead's structured output) were never part of a prompt and are left out of the tables; they come to A 19,288, C 2,922 estimated tokens per run.

### Injected context seen in the saved transcripts

Lines of type `attachment` by attachment type, summed over the lead and its subagents. `estimated` is chars/4 of the text the transcript records as rendered into the prompt (0 when it records none).

| run | attachment type | lines | estimated tokens |
|---|---|---|---|
| workers-8/A-1 | agent_listing_delta | 25 | 9,800 |
| workers-8/A-1 | budget_usd | 48 | 1,314 |
| workers-8/A-1 | credential_org | 25 | 0 |
| workers-8/A-1 | date | 25 | 525 |
| workers-8/A-1 | deferred_tools_record | 17 | 0 |
| workers-8/A-1 | environment | 25 | 5,500 |
| workers-8/A-1 | model | 25 | 1,150 |
| workers-8/A-1 | prompt_snapshot | 50 | 0 |
| workers-8/A-1 | remote_session_change | 25 | 3,800 |
| workers-8/A-1 | session_context | 25 | 167 |
| workers-8/A-1 | structured_output | 1 | 0 |
| workers-8/A-2 | agent_listing_delta | 25 | 9,800 |
| workers-8/A-2 | budget_usd | 38 | 1,020 |
| workers-8/A-2 | credential_org | 25 | 0 |
| workers-8/A-2 | date | 25 | 525 |
| workers-8/A-2 | deferred_tools_record | 26 | 0 |
| workers-8/A-2 | environment | 25 | 5,500 |
| workers-8/A-2 | model | 25 | 1,150 |
| workers-8/A-2 | prompt_snapshot | 50 | 0 |
| workers-8/A-2 | remote_session_change | 25 | 3,800 |
| workers-8/A-2 | session_context | 25 | 167 |
| workers-8/A-2 | structured_output | 1 | 0 |
| workers-8/A-3 | agent_listing_delta | 27 | 10,584 |
| workers-8/A-3 | budget_usd | 40 | 1,070 |
| workers-8/A-3 | credential_org | 27 | 0 |
| workers-8/A-3 | date | 27 | 567 |
| workers-8/A-3 | deferred_tools_record | 26 | 0 |
| workers-8/A-3 | environment | 27 | 5,940 |
| workers-8/A-3 | model | 27 | 1,242 |
| workers-8/A-3 | prompt_snapshot | 54 | 0 |
| workers-8/A-3 | remote_session_change | 27 | 4,104 |
| workers-8/A-3 | session_context | 27 | 167 |
| workers-8/A-3 | structured_output | 1 | 0 |
| workers-8/C-1 | agent_listing_delta | 1 | 592 |
| workers-8/C-1 | budget_usd | 225 | 6,427 |
| workers-8/C-1 | command_permissions | 1 | 0 |
| workers-8/C-1 | credential_org | 25 | 0 |
| workers-8/C-1 | date | 25 | 525 |
| workers-8/C-1 | deferred_tools_delta | 2 | 354 |
| workers-8/C-1 | deferred_tools_record | 98 | 0 |
| workers-8/C-1 | environment | 25 | 5,500 |
| workers-8/C-1 | mcp_instructions_delta | 25 | 5,450 |
| workers-8/C-1 | model | 25 | 1,150 |
| workers-8/C-1 | prompt_snapshot | 50 | 0 |
| workers-8/C-1 | remote_session_change | 25 | 3,800 |
| workers-8/C-1 | session_context | 25 | 167 |
| workers-8/C-1 | silent_turn_reminder | 2 | 78 |
| workers-8/C-1 | structured_output | 1 | 0 |
| workers-8/C-2 | agent_listing_delta | 1 | 592 |
| workers-8/C-2 | budget_usd | 197 | 5,613 |
| workers-8/C-2 | command_permissions | 1 | 0 |
| workers-8/C-2 | credential_org | 25 | 0 |
| workers-8/C-2 | date | 25 | 525 |
| workers-8/C-2 | deferred_tools_delta | 2 | 354 |
| workers-8/C-2 | deferred_tools_record | 63 | 0 |
| workers-8/C-2 | environment | 25 | 5,500 |
| workers-8/C-2 | mcp_instructions_delta | 25 | 5,450 |
| workers-8/C-2 | model | 25 | 1,150 |
| workers-8/C-2 | prompt_snapshot | 50 | 0 |
| workers-8/C-2 | remote_session_change | 25 | 3,800 |
| workers-8/C-2 | session_context | 25 | 167 |
| workers-8/C-2 | silent_turn_reminder | 1 | 39 |
| workers-8/C-2 | structured_output | 1 | 0 |
| h5-sweep-c8r/C-1 | agent_listing_delta | 1 | 592 |
| h5-sweep-c8r/C-1 | budget_usd | 220 | 6,320 |
| h5-sweep-c8r/C-1 | command_permissions | 1 | 0 |
| h5-sweep-c8r/C-1 | credential_org | 25 | 0 |
| h5-sweep-c8r/C-1 | date | 25 | 525 |
| h5-sweep-c8r/C-1 | deferred_tools_delta | 2 | 354 |
| h5-sweep-c8r/C-1 | deferred_tools_record | 92 | 0 |
| h5-sweep-c8r/C-1 | environment | 25 | 5,475 |
| h5-sweep-c8r/C-1 | mcp_instructions_delta | 25 | 5,450 |
| h5-sweep-c8r/C-1 | model | 25 | 1,150 |
| h5-sweep-c8r/C-1 | prompt_snapshot | 50 | 0 |
| h5-sweep-c8r/C-1 | remote_session_change | 25 | 3,800 |
| h5-sweep-c8r/C-1 | session_context | 25 | 167 |
| h5-sweep-c8r/C-1 | silent_turn_reminder | 1 | 39 |
| h5-sweep-c8r/C-1 | structured_output | 1 | 0 |
