# Benchmark cost breakdown

Written by `python -m bench.analyze_cost`. Every number is labelled. **Measured** numbers come from the API usage in the run's stream or from evals.jsonl. **Estimated** numbers are the chars/4 estimate of block text (`benchbook.tokens.estimate_tokens`). A measured increase of an agent's prompt between two model calls is split between the blocks that entered in between by their share of the estimate. That split is the only estimated part of a measured figure, and the `unsplit` column of section 4 shows how much needed none.

## Runs

| run | model calls (lead + subagents) | session tokens (measured) | lead tokens (measured) | prompt classes reconcile | all classes reconcile | usage source |
|---|---|---|---|---|---|---|
| workers-16/A-1 | 4 + 138 | 1,328,099 | 235,490 | yes | yes | transcripts |
| workers-16/A-2 | 4 + 147 | 1,355,927 | 248,622 | yes | yes | transcripts |
| workers-16/A-3 | 4 + 168 | 1,475,219 | 239,121 | yes | yes | transcripts |
| 16-C-1-try1/C-1 | 17 + 523 | 7,097,021 | 741,103 | yes | yes | transcripts |
| 16-C-2-try2/C-1 | 21 + 435 | 6,445,549 | 1,278,157 | yes | yes | transcripts |
| 16-C-3-try2/C-1 | 20 + 448 | 6,619,988 | 1,153,301 | yes | yes | transcripts |

6 of 6 runs reconcile per-message usage with the final totals in every token class. 6 of 6 reconcile the three prompt classes, which is what the attribution below uses. Session tokens are the final result event's `modelUsage` (lead and subagents). Lead tokens are its `usage`, which covers the lead's own API calls only and is what `tokens total` in report.md shows. A stream message carries the output count from the start of its response, so output tokens reconcile only from saved transcripts.

## 1. Session totals per configuration

Mean per run. All measured.

| configuration | runs | session tokens | lead prompt tokens | subagent prompt tokens | output tokens | lead calls | subagent calls | subagents | first prompt of the lead | first prompt of a subagent |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 1,386,415 | 186,523 | 1,057,406 | 142,486 | 4.0 | 151.0 | 48.0 | 8,519 | 5,805 |
| C | 3 | 6,720,853 | 1,015,691 | 5,473,646 | 231,515 | 19.3 | 468.7 | 47.3 | 13,995 | 7,671 |

Token classes of the session, mean per run (measured). Cache reads are billed at a fraction of the other input classes, so the token ratio between configurations is not their cost ratio.

| configuration | input | cache creation | cache read | output | cache read share | API cost (USD) |
|---|---|---|---|---|---|---|
| A | 310 | 249,269 | 994,350 | 142,486 | 71.7% | 2.2659 |
| C | 976 | 482,016 | 6,006,346 | 231,515 | 89.4% | 4.2516 |

## 2. Prompt tokens by what was read

Mean per run, with the share of the configuration's prompt tokens. A block is counted once for every model call whose prompt contained it. Totals are measured; the split inside one increase is by character share.

| what | A | C |
|---|---|---|
| first prompt of the lead, read by each of its calls | 34,076 (2.7%) | 274,296 (4.2%) |
| first prompt of each subagent, read by each of its calls | 876,768 (70.5%) | 3,595,142 (55.4%) |
| benchbook check_tried | 0 (0.0%) | 307,176 (4.7%) |
| benchbook record_experiment | 0 (0.0%) | 377,574 (5.8%) |
| benchbook brief | 0 (0.0%) | 391,642 (6.0%) |
| benchbook claims | 0 (0.0%) | 8,356 (0.1%) |
| benchbook tasks | 0 (0.0%) | 782,232 (12.1%) |
| benchbook other | 0 (0.0%) | 25,869 (0.4%) |
| evaluation Bash | 179,895 (14.5%) | 394,769 (6.1%) |
| other Bash | 0 (0.0%) | 36,586 (0.6%) |
| file Write | 0 (0.0%) | 6,214 (0.1%) |
| subagent delegation | 37,970 (3.1%) | 39,371 (0.6%) |
| subagent report | 114,477 (9.2%) | 240,822 (3.7%) |
| model text and thinking | 743 (0.1%) | 9,287 (0.1%) |
| **all prompt tokens (measured)** | 1,243,929 | 6,489,337 |

## 3. Model calls by what triggered them

Mean per run: model calls, and the prompt tokens those calls were billed (measured). A call is triggered by the tool calls of the response before it; a response with several kinds of tool call splits its follow-up call evenly between them.

| trigger | A | C |
|---|---|---|
| benchbook check_tried | 0.0 calls, 0 (0.0%) | 70.2 calls, 824,490 (12.7%) |
| benchbook record_experiment | 0.0 calls, 0 (0.0%) | 83.3 calls, 1,142,354 (17.6%) |
| benchbook brief | 0.0 calls, 0 (0.0%) | 21.7 calls, 221,443 (3.4%) |
| benchbook claims | 0.0 calls, 0 (0.0%) | 3.9 calls, 314,742 (4.9%) |
| benchbook tasks | 0.0 calls, 0 (0.0%) | 92.5 calls, 1,247,403 (19.2%) |
| benchbook other | 0.0 calls, 0 (0.0%) | 3.4 calls, 197,813 (3.0%) |
| evaluation Bash | 103.0 calls, 778,755 (62.6%) | 158.2 calls, 1,914,326 (29.5%) |
| other Bash | 0.0 calls, 0 (0.0%) | 2.8 calls, 38,180 (0.6%) |
| file Write | 0.0 calls, 0 (0.0%) | 0.7 calls, 20,887 (0.3%) |
| subagent delegation | 3.0 calls, 178,004 (14.3%) | 3.0 calls, 190,611 (2.9%) |
| start of an agent | 49.0 calls, 287,170 (23.1%) | 48.3 calls, 377,089 (5.8%) |

## 4. Context growth by block category

Mean per run. `estimated` is chars/4 of the blocks, `measured` is their share of the measured prompt increases, `unsplit` is the part of `measured` from increases with a single category.

| configuration | category | blocks | estimated tokens | measured tokens | unsplit | measured / estimated |
|---|---|---|---|---|---|---|
| A | evaluation Bash | 544.7 | 33,013 | 119,400 | 88,559 | 3.62 |
| A | subagent delegation | 48.0 | 9,407 | 18,548 | 0 | 1.97 |
| A | subagent report | 48.0 | 28,631 | 56,442 | 0 | 1.97 |
| A | model text and thinking | 39.0 | 244 | 682 | 0 | 2.79 |
| C | benchbook check_tried | 339.3 | 15,476 | 52,007 | 1,926 | 3.36 |
| C | benchbook record_experiment | 466.7 | 30,447 | 110,796 | 44,441 | 3.64 |
| C | benchbook brief | 84.7 | 18,816 | 47,278 | 628 | 2.51 |
| C | benchbook claims | 8.7 | 488 | 1,722 | 1,416 | 3.53 |
| C | benchbook tasks | 230.0 | 36,055 | 92,692 | 77,774 | 2.57 |
| C | benchbook other | 13.3 | 1,153 | 3,252 | 2,231 | 2.82 |
| C | evaluation Bash | 480.7 | 29,083 | 86,076 | 42,905 | 2.96 |
| C | other Bash | 9.3 | 163 | 2,397 | 2,069 | 14.74 |
| C | file Write | 1.3 | 158 | 591 | 591 | 3.73 |
| C | subagent delegation | 47.3 | 2,310 | 4,409 | 0 | 1.91 |
| C | subagent report | 47.3 | 13,277 | 25,367 | 0 | 1.91 |
| C | model text and thinking | 97.7 | 610 | 1,842 | 0 | 3.02 |

## 5. The lead's largest prompt

Mean per run: the first prompt plus the measured increases up to the largest prompt, split by character share.

| part | A | C |
|---|---|---|
| first prompt (measured) | 8,519 | 13,995 |
| benchbook brief | 0 | 338 |
| benchbook claims | 0 | 1,722 |
| benchbook tasks | 0 | 33,026 |
| benchbook other | 0 | 3,252 |
| evaluation Bash | 0 | 823 |
| other Bash | 0 | 2,184 |
| file Write | 0 | 591 |
| subagent delegation | 18,548 | 4,409 |
| subagent report | 56,442 | 25,367 |
| model text and thinking | 0 | 204 |
| **largest prompt (measured)** | 83,509 | 85,913 |

## 6. Repeated and nearby evaluations

From evals.jsonl (measured). Each evaluation is compared with the ones that finished before it in the same run. `near` counts evaluations with no earlier identical config but an earlier one with the same non-numeric fields and every numeric field within the tolerance.

| run | evaluations | duplicate ratio | exact repeat | replicate | near at 10% | near at 25% | near at 50% |
|---|---|---|---|---|---|---|---|
| workers-16/A-1 | 235 | 53.2% | 1 | 102 | 5 | 30 | 102 |
| workers-16/A-2 | 240 | 54.6% | 3 | 99 | 1 | 28 | 90 |
| workers-16/A-3 | 240 | 52.5% | 0 | 99 | 0 | 27 | 93 |
| 16-C-1-try1/C-1 | 234 | 26.9% | 0 | 52 | 1 | 39 | 110 |
| 16-C-2-try2/C-1 | 236 | 69.5% | 0 | 123 | 0 | 7 | 56 |
| 16-C-3-try2/C-1 | 230 | 54.3% | 1 | 85 | 0 | 22 | 89 |

## Hypotheses (spec §10)

A verdict is computed from the stated measure: supported at 50% or more, contradicted below 20%, partly supported in between, undetermined when an input it needs is missing.

### Hypothesis 1: Recording and looking up spend agent turns: every attempt costs a separate check_tried and a separate record call.

**Verdict: partly supported** (prompt tokens of the model calls that follow check_tried or record_experiment, as a share of C's session-token gap over A: 36.9%).

- C made 169.7 check_tried and 233.3 record_experiment tool calls for 233.3 evaluations per run (measured). They were followed by 153.5 model calls, 2.6 tool calls per model call: the agents batch several lookups or records into one turn.
- Model calls triggered by check_tried or record_experiment: 153.5 of 488.0 per run (31.5%), billed 1,966,844 prompt tokens (29.3% of C's session tokens; measured).
- Model calls triggered by any benchbook tool: 275.0 per run (56.4%), billed 3,948,245 prompt tokens (58.7% of C's session tokens; measured).
- C used 5,334,438 more session tokens per run than A (6,720,853 against 1,386,415; measured). The calls that follow a lookup or a record account for 36.9% of that gap, the calls that follow any benchbook tool for 74.0%.
- Model calls per run: C 488.0, A 155.0. First prompt of a subagent: C 7,671, A 5,805 tokens (measured), read again by every one of its calls.

### Hypothesis 2: The exact hash misses nearby configurations; agents that read result files avoid similar configurations by eye.

**Verdict: partly supported** (near configs (within 25%) as a share of C's repeated or nearly repeated evaluations, set to zero when B has at least as many near configs as C: 20.7%).

- C per run: 0.3 exact repeats (same config, code and seed), 86.7 replicates (same config and code, a new seed) and 22.7 evaluations within 25% of an earlier config on every numeric field, out of 233.3 (measured from evals.jsonl).
- Of C's evaluations that repeat or nearly repeat an earlier one, 20.7% are near configs, which an exact hash cannot see; the rest have an identical config, which it does see.
- The benchmark's duplicate ratio (50.3% in C) counts an evaluation when its (config, code) occurs twice, whatever the seed: replicates raise it and near configs never do.
- Near configs per run by tolerance: 10%: A 2.0, C 0.3; 25%: A 28.3, C 22.7; 50%: A 95.0, C 85.0.

### Hypothesis 3: The brief grows the lead's context.

**Verdict: contradicted** (tokens of brief calls and results in the lead's largest prompt, as a share of the gap between C's and A's largest lead prompt: 14.1%).

- C's largest lead prompt: 85,913 tokens per run (measured), of which 13,995 were there at the first call (system prompt, tool definitions, the lead skill and the task).
- Of the growth after that, brief calls and results account for 338 tokens, all benchbook tool calls and results for 38,339, subagent reports for 25,367 (measured increases, split inside an increase by character share).
- C's largest lead prompt exceeds A's by 2,404 tokens (83,509 in A). The brief accounts for 14.1% of that gap, all benchbook tool calls and results for 1595.0%, and the larger first prompt (+5,476 tokens) for 227.8%. The parts need not add up to the gap: A's lead prompt holds 56,442 tokens of subagent reports against C's 25,367.

## What these numbers do not cover

- System prompt, tool definitions, skill text and system reminders are not in the stream. They are inside each agent's first prompt (measured as a whole) and inside the increases, where they are split over the visible blocks or, with none, counted as `not in the stream`.
- Thinking is forwarded without its text, so its tokens are split over the visible blocks of the same increase.
- Output tokens are reported per session and per lead only. Blocks written after an agent's last model call (a subagent's closing report, the lead's structured output) were never part of a prompt and are left out of the tables; they come to A 38,048, C 5,389 estimated tokens per run.

### Injected context seen in the saved transcripts

Lines of type `attachment` by attachment type, summed over the lead and its subagents. `estimated` is chars/4 of the text the transcript records as rendered into the prompt (0 when it records none).

| run | attachment type | lines | estimated tokens |
|---|---|---|---|
| workers-16/A-1 | agent_listing_delta | 49 | 19,208 |
| workers-16/A-1 | budget_usd | 94 | 2,651 |
| workers-16/A-1 | credential_org | 49 | 0 |
| workers-16/A-1 | date | 49 | 1,029 |
| workers-16/A-1 | deferred_tools_record | 60 | 0 |
| workers-16/A-1 | environment | 49 | 10,878 |
| workers-16/A-1 | model | 49 | 2,254 |
| workers-16/A-1 | prompt_snapshot | 98 | 0 |
| workers-16/A-1 | remote_session_change | 49 | 7,448 |
| workers-16/A-1 | session_context | 49 | 167 |
| workers-16/A-1 | structured_output | 1 | 0 |
| workers-16/A-2 | agent_listing_delta | 49 | 19,208 |
| workers-16/A-2 | budget_usd | 103 | 2,873 |
| workers-16/A-2 | credential_org | 49 | 0 |
| workers-16/A-2 | date | 49 | 1,029 |
| workers-16/A-2 | deferred_tools_record | 35 | 0 |
| workers-16/A-2 | environment | 49 | 10,878 |
| workers-16/A-2 | model | 49 | 2,254 |
| workers-16/A-2 | prompt_snapshot | 98 | 0 |
| workers-16/A-2 | remote_session_change | 49 | 7,448 |
| workers-16/A-2 | session_context | 49 | 167 |
| workers-16/A-2 | structured_output | 1 | 0 |
| workers-16/A-3 | agent_listing_delta | 49 | 19,208 |
| workers-16/A-3 | budget_usd | 124 | 3,438 |
| workers-16/A-3 | credential_org | 49 | 0 |
| workers-16/A-3 | date | 49 | 1,029 |
| workers-16/A-3 | deferred_tools_record | 93 | 0 |
| workers-16/A-3 | environment | 49 | 10,878 |
| workers-16/A-3 | model | 49 | 2,254 |
| workers-16/A-3 | prompt_snapshot | 98 | 0 |
| workers-16/A-3 | remote_session_change | 49 | 7,448 |
| workers-16/A-3 | session_context | 49 | 167 |
| workers-16/A-3 | structured_output | 1 | 0 |
| 16-C-1-try1/C-1 | agent_listing_delta | 1 | 592 |
| 16-C-1-try1/C-1 | budget_usd | 492 | 14,099 |
| 16-C-1-try1/C-1 | command_permissions | 1 | 0 |
| 16-C-1-try1/C-1 | credential_org | 49 | 0 |
| 16-C-1-try1/C-1 | date | 49 | 1,029 |
| 16-C-1-try1/C-1 | deferred_tools_delta | 2 | 354 |
| 16-C-1-try1/C-1 | deferred_tools_record | 194 | 0 |
| 16-C-1-try1/C-1 | environment | 49 | 10,780 |
| 16-C-1-try1/C-1 | mcp_instructions_delta | 49 | 10,682 |
| 16-C-1-try1/C-1 | model | 49 | 2,254 |
| 16-C-1-try1/C-1 | prompt_snapshot | 98 | 0 |
| 16-C-1-try1/C-1 | remote_session_change | 49 | 7,448 |
| 16-C-1-try1/C-1 | session_context | 49 | 167 |
| 16-C-1-try1/C-1 | silent_turn_reminder | 2 | 78 |
| 16-C-1-try1/C-1 | structured_output | 1 | 0 |
| 16-C-2-try2/C-1 | agent_listing_delta | 1 | 592 |
| 16-C-2-try2/C-1 | budget_usd | 408 | 11,997 |
| 16-C-2-try2/C-1 | command_permissions | 1 | 0 |
| 16-C-2-try2/C-1 | credential_org | 49 | 0 |
| 16-C-2-try2/C-1 | date | 49 | 1,029 |
| 16-C-2-try2/C-1 | deferred_tools_record | 162 | 0 |
| 16-C-2-try2/C-1 | environment | 49 | 10,780 |
| 16-C-2-try2/C-1 | mcp_instructions_delta | 49 | 10,682 |
| 16-C-2-try2/C-1 | model | 49 | 2,254 |
| 16-C-2-try2/C-1 | prompt_snapshot | 98 | 0 |
| 16-C-2-try2/C-1 | remote_session_change | 49 | 7,448 |
| 16-C-2-try2/C-1 | session_context | 49 | 167 |
| 16-C-2-try2/C-1 | silent_turn_reminder | 1 | 39 |
| 16-C-2-try2/C-1 | structured_output | 1 | 0 |
| 16-C-3-try2/C-1 | agent_listing_delta | 1 | 592 |
| 16-C-3-try2/C-1 | budget_usd | 422 | 12,299 |
| 16-C-3-try2/C-1 | command_permissions | 1 | 0 |
| 16-C-3-try2/C-1 | credential_org | 47 | 0 |
| 16-C-3-try2/C-1 | date | 47 | 987 |
| 16-C-3-try2/C-1 | deferred_tools_record | 167 | 0 |
| 16-C-3-try2/C-1 | environment | 47 | 10,340 |
| 16-C-3-try2/C-1 | mcp_instructions_delta | 47 | 10,246 |
| 16-C-3-try2/C-1 | model | 47 | 2,162 |
| 16-C-3-try2/C-1 | prompt_snapshot | 94 | 0 |
| 16-C-3-try2/C-1 | remote_session_change | 47 | 7,144 |
| 16-C-3-try2/C-1 | session_context | 47 | 167 |
| 16-C-3-try2/C-1 | silent_turn_reminder | 3 | 117 |
| 16-C-3-try2/C-1 | structured_output | 1 | 0 |
