# Benchmark cost breakdown

Written by `python -m bench.analyze_cost`. Every number is labelled. **Measured** numbers come from the API usage in the run's stream or from evals.jsonl. **Estimated** numbers are the chars/4 estimate of block text (`benchbook.tokens.estimate_tokens`). A measured increase of an agent's prompt between two model calls is split between the blocks that entered in between by their share of the estimate. That split is the only estimated part of a measured figure, and the `unsplit` column of section 4 shows how much needed none.

## Runs

| run | model calls (lead + subagents) | session tokens (measured) | lead tokens (measured) | prompt classes reconcile | all classes reconcile | usage source |
|---|---|---|---|---|---|---|
| A-1 | 4 + 39 | 399,655 | 89,593 | yes | yes | transcripts |
| A-2 | 4 + 28 | 328,030 | 95,338 | yes | yes | transcripts |
| A-3 | 4 + 29 | 341,978 | 97,266 | yes | yes | transcripts |
| B-1 | 10 + 98 | 1,149,907 | 204,519 | yes | yes | transcripts |
| B-2 | 8 + 97 | 1,097,595 | 175,016 | yes | yes | transcripts |
| B-3 | 9 + 99 | 1,165,768 | 192,049 | yes | yes | transcripts |
| C-1 | 11 + 93 | 1,372,821 | 270,289 | yes | yes | transcripts |
| C-2 | 11 + 105 | 1,528,212 | 277,232 | yes | yes | transcripts |
| C-3 | 16 + 99 | 1,597,285 | 430,792 | yes | yes | transcripts |

9 of 9 runs reconcile per-message usage with the final totals in every token class. 9 of 9 reconcile the three prompt classes, which is what the attribution below uses. Session tokens are the final result event's `modelUsage` (lead and subagents). Lead tokens are its `usage`, which covers the lead's own API calls only and is what `tokens total` in report.md shows. A stream message carries the output count from the start of its response, so output tokens reconcile only from saved transcripts.

## Against an earlier batch

Mean per run. The earlier values are that batch's metrics.json; these are recomputed from the runs above with the same definitions (measured).

| configuration | metric | earlier | this batch | this / earlier |
|---|---|---|---|---|
| A | evaluations | 60 | 60 | 1.00 |
| A | duplicate ratio | 0.4056 | 0.2944 | 0.73 |
| A | lead context max (tokens) | 29,405 | 30,499 | 1.04 |
| A | tokens total (lead, as in report.md) | 91,782 | 94,066 | 1.02 |
| A | API cost (USD) | 0.5958 | 0.6346 | 1.07 |
| B | evaluations | 60 | 58.33 | 0.97 |
| B | duplicate ratio | 0.1444 | 0.1611 | 1.12 |
| B | lead context max (tokens) | 34,010 | 35,577 | 1.05 |
| B | tokens total (lead, as in report.md) | 209,476 | 190,528 | 0.91 |
| B | API cost (USD) | 0.8778 | 0.9603 | 1.09 |
| C | evaluations | 58.67 | 59.33 | 1.01 |
| C | duplicate ratio | 0.3009 | 0.2249 | 0.75 |
| C | lead context max (tokens) | 39,861 | 33,058 | 0.83 |
| C | tokens total (lead, as in report.md) | 471,683 | 326,104 | 0.69 |
| C | API cost (USD) | 1.175 | 1.132 | 0.96 |

## 1. Session totals per configuration

Mean per run. All measured.

| configuration | runs | session tokens | lead prompt tokens | subagent prompt tokens | output tokens | lead calls | subagent calls | subagents | first prompt of the lead | first prompt of a subagent |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 356,554 | 78,831 | 238,633 | 39,090 | 4.0 | 32.0 | 12.0 | 9,014 | 6,266 |
| B | 3 | 1,137,757 | 182,353 | 901,511 | 53,893 | 9.0 | 98.0 | 12.0 | 9,012 | 6,302 |
| C | 3 | 1,499,439 | 316,662 | 1,120,202 | 62,576 | 12.7 | 99.0 | 12.0 | 15,913 | 7,326 |

Token classes of the session, mean per run (measured). Cache reads are billed at a fraction of the other input classes, so the token ratio between configurations is not their cost ratio.

| configuration | input | cache creation | cache read | output | cache read share | API cost (USD) |
|---|---|---|---|---|---|---|
| A | 72 | 71,404 | 245,989 | 39,090 | 69.0% | 0.6346 |
| B | 214 | 111,336 | 972,314 | 53,893 | 85.5% | 0.9603 |
| C | 223 | 134,218 | 1,302,423 | 62,576 | 86.9% | 1.1318 |

## 2. Prompt tokens by what was read

Mean per run, with the share of the configuration's prompt tokens. A block is counted once for every model call whose prompt contained it. Totals are measured; the split inside one increase is by character share.

| what | A | B | C |
|---|---|---|---|
| first prompt of the lead, read by each of its calls | 36,057 (11.4%) | 81,108 (7.5%) | 201,547 (14.0%) |
| first prompt of each subagent, read by each of its calls | 200,016 (63.0%) | 617,491 (57.0%) | 725,274 (50.5%) |
| benchbook check_tried | 0 (0.0%) | 0 (0.0%) | 82,005 (5.7%) |
| benchbook record_experiment | 0 (0.0%) | 0 (0.0%) | 61,375 (4.3%) |
| benchbook brief | 0 (0.0%) | 0 (0.0%) | 91,130 (6.3%) |
| benchbook tasks | 0 (0.0%) | 0 (0.0%) | 121,567 (8.5%) |
| benchbook other | 0 (0.0%) | 0 (0.0%) | 11,496 (0.8%) |
| evaluation Bash | 38,445 (12.1%) | 181,558 (16.8%) | 80,354 (5.6%) |
| other Bash | 0 (0.0%) | 15,036 (1.4%) | 10,424 (0.7%) |
| file Read | 0 (0.0%) | 4,915 (0.5%) | 0 (0.0%) |
| file Write | 0 (0.0%) | 96,602 (8.9%) | 0 (0.0%) |
| subagent delegation | 11,063 (3.5%) | 25,051 (2.3%) | 6,748 (0.5%) |
| subagent report | 31,577 (9.9%) | 60,625 (5.6%) | 41,608 (2.9%) |
| model text and thinking | 306 (0.1%) | 1,479 (0.1%) | 3,336 (0.2%) |
| **all prompt tokens (measured)** | 317,465 | 1,083,864 | 1,436,864 |

## 3. Model calls by what triggered them

Mean per run: model calls, and the prompt tokens those calls were billed (measured). A call is triggered by the tool calls of the response before it; a response with several kinds of tool call splits its follow-up call evenly between them.

| trigger | A | B | C |
|---|---|---|---|
| benchbook check_tried | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 13.5 calls, 148,812 (10.4%) |
| benchbook record_experiment | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 13.3 calls, 197,630 (13.8%) |
| benchbook brief | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 6.5 calls, 66,564 (4.6%) |
| benchbook tasks | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 27.5 calls, 368,306 (25.6%) |
| benchbook other | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 3.8 calls, 109,275 (7.6%) |
| evaluation Bash | 20.0 calls, 163,446 (51.5%) | 50.7 calls, 446,145 (41.2%) | 24.3 calls, 293,141 (20.4%) |
| other Bash | 0.0 calls, 0 (0.0%) | 4.2 calls, 72,159 (6.7%) | 6.7 calls, 69,623 (4.8%) |
| file Read | 0.0 calls, 0 (0.0%) | 1.7 calls, 37,028 (3.4%) | 0.0 calls, 0 (0.0%) |
| file Write | 0.0 calls, 0 (0.0%) | 34.5 calls, 372,812 (34.4%) | 0.0 calls, 0 (0.0%) |
| subagent delegation | 3.0 calls, 69,817 (22.0%) | 3.0 calls, 71,084 (6.6%) | 3.0 calls, 79,688 (5.5%) |
| start of an agent | 13.0 calls, 84,202 (26.5%) | 13.0 calls, 84,636 (7.8%) | 13.0 calls, 103,825 (7.2%) |

## 4. Context growth by block category

Mean per run. `estimated` is chars/4 of the blocks, `measured` is their share of the measured prompt increases, `unsplit` is the part of `measured` from increases with a single category.

| configuration | category | blocks | estimated tokens | measured tokens | unsplit | measured / estimated |
|---|---|---|---|---|---|---|
| A | evaluation Bash | 110.0 | 7,512 | 29,552 | 22,745 | 3.93 |
| A | subagent delegation | 12.0 | 2,765 | 5,527 | 0 | 2.00 |
| A | subagent report | 12.0 | 7,952 | 15,883 | 0 | 2.00 |
| A | model text and thinking | 8.7 | 77 | 211 | 0 | 2.75 |
| B | evaluation Bash | 148.7 | 9,801 | 34,234 | 16,566 | 3.49 |
| B | other Bash | 10.7 | 2,113 | 5,189 | 4,742 | 2.46 |
| B | file Read | 4.0 | 747 | 1,891 | 1,651 | 2.53 |
| B | file Write | 116.0 | 12,148 | 32,397 | 6,487 | 2.67 |
| B | subagent delegation | 12.0 | 2,970 | 5,852 | 0 | 1.97 |
| B | subagent report | 12.0 | 7,241 | 14,329 | 0 | 1.98 |
| B | model text and thinking | 25.0 | 113 | 273 | 0 | 2.41 |
| C | benchbook check_tried | 109.3 | 4,751 | 16,362 | 1,764 | 3.44 |
| C | benchbook record_experiment | 118.7 | 7,697 | 27,882 | 25,542 | 3.62 |
| C | benchbook brief | 26.0 | 5,628 | 14,079 | 0 | 2.50 |
| C | benchbook tasks | 57.3 | 7,134 | 19,639 | 18,091 | 2.75 |
| C | benchbook other | 12.0 | 858 | 2,603 | 1,790 | 3.03 |
| C | evaluation Bash | 117.3 | 8,369 | 23,802 | 6,050 | 2.84 |
| C | other Bash | 26.0 | 600 | 1,572 | 51 | 2.62 |
| C | subagent delegation | 12.0 | 567 | 1,086 | 0 | 1.91 |
| C | subagent report | 12.0 | 3,261 | 6,246 | 0 | 1.92 |
| C | model text and thinking | 34.3 | 252 | 750 | 0 | 2.98 |

## 5. The lead's largest prompt

Mean per run: the first prompt plus the measured increases up to the largest prompt, split by character share.

| part | A | B | C |
|---|---|---|---|
| first prompt (measured) | 9,014 | 9,012 | 15,913 |
| benchbook brief | 0 | 0 | 762 |
| benchbook tasks | 0 | 0 | 6,173 |
| benchbook other | 0 | 0 | 2,603 |
| evaluation Bash | 0 | 101 | 0 |
| other Bash | 0 | 4,516 | 113 |
| file Read | 0 | 1,674 | 0 |
| subagent delegation | 5,527 | 5,852 | 1,086 |
| subagent report | 15,883 | 14,329 | 6,246 |
| model text and thinking | 75 | 93 | 162 |
| **largest prompt (measured)** | 30,499 | 35,577 | 33,058 |

## 6. Repeated and nearby evaluations

From evals.jsonl (measured). Each evaluation is compared with the ones that finished before it in the same run. `near` counts evaluations with no earlier identical config but an earlier one with the same non-numeric fields and every numeric field within the tolerance.

| run | evaluations | duplicate ratio | exact repeat | replicate | near at 10% | near at 25% | near at 50% |
|---|---|---|---|---|---|---|---|
| A-1 | 60 | 38.3% | 1 | 13 | 1 | 7 | 20 |
| A-2 | 60 | 38.3% | 0 | 18 | 0 | 8 | 18 |
| A-3 | 60 | 11.7% | 0 | 4 | 1 | 8 | 28 |
| B-1 | 60 | 18.3% | 0 | 8 | 0 | 9 | 25 |
| B-2 | 60 | 10.0% | 0 | 3 | 2 | 8 | 30 |
| B-3 | 55 | 20.0% | 0 | 6 | 0 | 2 | 19 |
| C-1 | 60 | 11.7% | 1 | 5 | 0 | 4 | 22 |
| C-2 | 60 | 31.7% | 1 | 11 | 0 | 0 | 15 |
| C-3 | 58 | 24.1% | 0 | 9 | 0 | 0 | 16 |

## Hypotheses (spec §10)

A verdict is computed from the stated measure: supported at 50% or more, contradicted below 20%, partly supported in between, undetermined when an input it needs is missing.

### Hypothesis 1: Recording and looking up spend agent turns: every attempt costs a separate check_tried and a separate record call.

**Verdict: partly supported** (prompt tokens of the model calls that follow check_tried or record_experiment, as a share of C's session-token gap over A: 30.3%).

- C made 54.7 check_tried and 59.3 record_experiment tool calls for 59.3 evaluations per run (measured). They were followed by 26.8 model calls, 4.2 tool calls per model call: the agents batch several lookups or records into one turn.
- Model calls triggered by check_tried or record_experiment: 26.8 of 111.7 per run (24.0%), billed 346,442 prompt tokens (23.1% of C's session tokens; measured).
- Model calls triggered by any benchbook tool: 64.7 per run (57.9%), billed 890,587 prompt tokens (59.4% of C's session tokens; measured).
- C used 1,142,885 more session tokens per run than A (1,499,439 against 356,554; measured). The calls that follow a lookup or a record account for 30.3% of that gap, the calls that follow any benchbook tool for 77.9%.
- Model calls per run: C 111.7, A 36.0. First prompt of a subagent: C 7,326, A 6,266 tokens (measured), read again by every one of its calls.

### Hypothesis 2: The exact hash misses nearby configurations; agents that read result files avoid similar configurations by eye.

**Verdict: contradicted** (near configs (within 25%) as a share of C's repeated or nearly repeated evaluations, set to zero when B has at least as many near configs as C: 0.0%).

- C per run: 0.7 exact repeats (same config, code and seed), 8.3 replicates (same config and code, a new seed) and 1.3 evaluations within 25% of an earlier config on every numeric field, out of 59.3 (measured from evals.jsonl).
- Of C's evaluations that repeat or nearly repeat an earlier one, 12.9% are near configs, which an exact hash cannot see; the rest have an identical config, which it does see.
- The benchmark's duplicate ratio (22.5% in C) counts an evaluation when its (config, code) occurs twice, whatever the seed: replicates raise it and near configs never do.
- Near configs per run at 25%: B 6.3, C 1.3, A 7.7. B does not have fewer, so reading the result files did not make agents avoid them.
- Near configs per run by tolerance: 10%: A 0.7, B 0.7, C 0.0; 25%: A 7.7, B 6.3, C 1.3; 50%: A 22.0, B 24.7, C 17.7.

### Hypothesis 3: The brief grows the lead's context.

**Verdict: partly supported** (tokens of brief calls and results in the lead's largest prompt, as a share of the gap between C's and A's largest lead prompt: 29.8%).

- C's largest lead prompt: 33,058 tokens per run (measured), of which 15,913 were there at the first call (system prompt, tool definitions, the lead skill and the task).
- Of the growth after that, brief calls and results account for 762 tokens, all benchbook tool calls and results for 9,538, subagent reports for 6,246 (measured increases, split inside an increase by character share).
- C's largest lead prompt exceeds A's by 2,559 tokens (30,499 in A). The brief accounts for 29.8% of that gap, all benchbook tool calls and results for 372.8%, and the larger first prompt (+6,899 tokens) for 269.6%. The parts need not add up to the gap: A's lead prompt holds 15,883 tokens of subagent reports against C's 6,246.

## What these numbers do not cover

- System prompt, tool definitions, skill text and system reminders are not in the stream. They are inside each agent's first prompt (measured as a whole) and inside the increases, where they are split over the visible blocks or, with none, counted as `not in the stream`.
- Thinking is forwarded without its text, so its tokens are split over the visible blocks of the same increase.
- Output tokens are reported per session and per lead only. Blocks written after an agent's last model call (a subagent's closing report, the lead's structured output) were never part of a prompt and are left out of the tables; they come to A 10,495, B 5,752, C 1,599 estimated tokens per run.

### Injected context seen in the saved transcripts

Lines of type `attachment` by attachment type, summed over the lead and its subagents. `estimated` is chars/4 of the text the transcript records as rendered into the prompt (0 when it records none).

| run | attachment type | lines | estimated tokens |
|---|---|---|---|
| A-1 | agent_listing_delta | 13 | 5,538 |
| A-1 | budget_usd | 31 | 866 |
| A-1 | credential_org | 13 | 0 |
| A-1 | date | 13 | 273 |
| A-1 | deferred_tools_record | 8 | 0 |
| A-1 | environment | 13 | 3,133 |
| A-1 | model | 13 | 598 |
| A-1 | prompt_snapshot | 26 | 0 |
| A-1 | remote_session_change | 13 | 1,976 |
| A-1 | session_context | 13 | 168 |
| A-1 | structured_output | 1 | 0 |
| A-2 | agent_listing_delta | 13 | 5,538 |
| A-2 | budget_usd | 20 | 536 |
| A-2 | credential_org | 13 | 0 |
| A-2 | date | 13 | 273 |
| A-2 | deferred_tools_record | 7 | 0 |
| A-2 | environment | 13 | 3,133 |
| A-2 | model | 13 | 598 |
| A-2 | prompt_snapshot | 26 | 0 |
| A-2 | remote_session_change | 13 | 1,976 |
| A-2 | session_context | 13 | 168 |
| A-2 | structured_output | 1 | 0 |
| A-3 | agent_listing_delta | 13 | 5,538 |
| A-3 | budget_usd | 21 | 557 |
| A-3 | credential_org | 13 | 0 |
| A-3 | date | 13 | 273 |
| A-3 | deferred_tools_record | 14 | 0 |
| A-3 | environment | 13 | 3,133 |
| A-3 | model | 13 | 598 |
| A-3 | prompt_snapshot | 26 | 0 |
| A-3 | remote_session_change | 13 | 1,976 |
| A-3 | session_context | 13 | 168 |
| A-3 | structured_output | 1 | 0 |
| B-1 | agent_listing_delta | 13 | 5,538 |
| B-1 | budget_usd | 96 | 2,564 |
| B-1 | credential_org | 13 | 0 |
| B-1 | date | 13 | 273 |
| B-1 | deferred_tools_record | 54 | 0 |
| B-1 | environment | 13 | 3,133 |
| B-1 | model | 13 | 598 |
| B-1 | prompt_snapshot | 26 | 0 |
| B-1 | remote_session_change | 13 | 1,976 |
| B-1 | session_context | 13 | 168 |
| B-1 | structured_output | 1 | 0 |
| B-2 | agent_listing_delta | 13 | 5,538 |
| B-2 | budget_usd | 93 | 2,563 |
| B-2 | credential_org | 13 | 0 |
| B-2 | date | 13 | 273 |
| B-2 | deferred_tools_record | 29 | 0 |
| B-2 | environment | 13 | 3,133 |
| B-2 | model | 13 | 598 |
| B-2 | prompt_snapshot | 26 | 0 |
| B-2 | remote_session_change | 13 | 1,976 |
| B-2 | session_context | 13 | 168 |
| B-2 | structured_output | 1 | 0 |
| B-3 | agent_listing_delta | 13 | 5,538 |
| B-3 | budget_usd | 96 | 2,644 |
| B-3 | credential_org | 13 | 0 |
| B-3 | date | 13 | 273 |
| B-3 | deferred_tools_record | 8 | 0 |
| B-3 | environment | 14 | 3,250 |
| B-3 | model | 13 | 598 |
| B-3 | prompt_snapshot | 26 | 0 |
| B-3 | remote_session_change | 13 | 1,976 |
| B-3 | session_context | 13 | 168 |
| B-3 | structured_output | 1 | 0 |
| C-1 | agent_listing_delta | 1 | 616 |
| C-1 | budget_usd | 92 | 2,546 |
| C-1 | command_permissions | 1 | 0 |
| C-1 | credential_org | 13 | 0 |
| C-1 | date | 13 | 273 |
| C-1 | deferred_tools_record | 5 | 0 |
| C-1 | environment | 13 | 3,133 |
| C-1 | mcp_instructions_delta | 13 | 2,821 |
| C-1 | model | 13 | 598 |
| C-1 | prompt_snapshot | 26 | 0 |
| C-1 | remote_session_change | 13 | 1,976 |
| C-1 | session_context | 13 | 168 |
| C-1 | silent_turn_reminder | 1 | 39 |
| C-1 | structured_output | 1 | 0 |
| C-2 | agent_listing_delta | 1 | 616 |
| C-2 | budget_usd | 104 | 2,918 |
| C-2 | command_permissions | 1 | 0 |
| C-2 | credential_org | 13 | 0 |
| C-2 | date | 13 | 273 |
| C-2 | deferred_tools_record | 12 | 0 |
| C-2 | environment | 13 | 3,133 |
| C-2 | mcp_instructions_delta | 13 | 2,821 |
| C-2 | model | 13 | 598 |
| C-2 | prompt_snapshot | 26 | 0 |
| C-2 | remote_session_change | 13 | 1,976 |
| C-2 | session_context | 13 | 168 |
| C-2 | silent_turn_reminder | 1 | 39 |
| C-2 | structured_output | 1 | 0 |
| C-3 | agent_listing_delta | 1 | 616 |
| C-3 | budget_usd | 103 | 2,806 |
| C-3 | command_permissions | 1 | 0 |
| C-3 | credential_org | 13 | 0 |
| C-3 | date | 13 | 273 |
| C-3 | deferred_tools_record | 7 | 0 |
| C-3 | environment | 13 | 3,133 |
| C-3 | mcp_instructions_delta | 13 | 2,821 |
| C-3 | model | 13 | 598 |
| C-3 | prompt_snapshot | 26 | 0 |
| C-3 | remote_session_change | 13 | 1,976 |
| C-3 | session_context | 13 | 168 |
| C-3 | silent_turn_reminder | 1 | 39 |
| C-3 | structured_output | 1 | 0 |
