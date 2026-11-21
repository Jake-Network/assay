# Benchmark cost breakdown

Written by `python -m bench.analyze_cost`. Every number is labelled. **Measured** numbers come from the API usage in the run's stream or from evals.jsonl. **Estimated** numbers are the chars/4 estimate of block text (`benchbook.tokens.estimate_tokens`). A measured increase of an agent's prompt between two model calls is split between the blocks that entered in between by their share of the estimate. That split is the only estimated part of a measured figure, and the `unsplit` column of section 4 shows how much needed none.

## Runs

| run | model calls (lead + subagents) | session tokens (measured) | lead tokens (measured) | prompt classes reconcile | all classes reconcile | usage source |
|---|---|---|---|---|---|---|
| sonnet55-ae81ddc-run2/A-1 | 4 + 26 | 305,879 | 92,927 | yes | no | stream (output_tokens -22,683) |
| sonnet55-ae81ddc-run2/A-2 | 4 + 40 | 419,129 | 91,342 | yes | no | stream (output_tokens -25,841) |
| sonnet55-ae81ddc-run2/A-3 | 4 + 32 | 346,941 | 91,078 | yes | no | stream (output_tokens -22,329) |
| sonnet55-ae81ddc-run2/B-1 | 11 + 96 | 1,134,476 | 251,815 | yes | no | stream (output_tokens -33,561) |
| sonnet55-ae81ddc-run2/B-2 | 9 + 91 | 1,029,464 | 193,633 | yes | no | stream (output_tokens -33,285) |
| sonnet55-ae81ddc-run2/B-3 | 8 + 94 | 1,056,393 | 182,979 | yes | no | stream (output_tokens -44,190) |
| sonnet55-ae81ddc-run2/C-1 | 15 + 108 | 1,916,860 | 545,436 | yes | no | stream (output_tokens -52,199) |
| sonnet55-ae81ddc-run2/C-2 | 14 + 97 | 1,613,926 | 398,490 | yes | no | stream (output_tokens -50,110) |
| sonnet55-ae81ddc-run2-C3/C-1 | 16 + 130 | 2,146,677 | 471,123 | yes | no | stream (output_tokens -53,471) |

0 of 9 runs reconcile per-message usage with the final totals in every token class. 9 of 9 reconcile the three prompt classes, which is what the attribution below uses. Session tokens are the final result event's `modelUsage` (lead and subagents). Lead tokens are its `usage`, which covers the lead's own API calls only and is what `tokens total` in report.md shows. A stream message carries the output count from the start of its response, so output tokens reconcile only from saved transcripts.

## Against an earlier batch

Mean per run. The earlier values are that batch's metrics.json; these are recomputed from the runs above with the same definitions (measured).

| configuration | metric | earlier | this batch | this / earlier |
|---|---|---|---|---|
| A | evaluations | 60 | 60 | 1.00 |
| A | duplicate ratio | 0.4056 | 0.4056 | 1.00 |
| A | lead context max (tokens) | 29,405 | 29,405 | 1.00 |
| A | tokens total (lead, as in report.md) | 91,782 | 91,782 | 1.00 |
| A | API cost (USD) | 0.5958 | 0.5958 | 1.00 |
| B | evaluations | 60 | 60 | 1.00 |
| B | duplicate ratio | 0.1444 | 0.1444 | 1.00 |
| B | lead context max (tokens) | 34,010 | 34,010 | 1.00 |
| B | tokens total (lead, as in report.md) | 209,476 | 209,476 | 1.00 |
| B | API cost (USD) | 0.8778 | 0.8778 | 1.00 |
| C | evaluations | 58.67 | 58.67 | 1.00 |
| C | duplicate ratio | 0.3009 | 0.3009 | 1.00 |
| C | lead context max (tokens) | 39,861 | 39,861 | 1.00 |
| C | tokens total (lead, as in report.md) | 471,683 | 471,683 | 1.00 |
| C | API cost (USD) | 1.175 | 1.175 | 1.00 |

## 1. Session totals per configuration

Mean per run. All measured.

| configuration | runs | session tokens | lead prompt tokens | subagent prompt tokens | output tokens | lead calls | subagent calls | subagents | first prompt of the lead | first prompt of a subagent |
|---|---|---|---|---|---|---|---|---|---|---|
| A | 3 | 357,316 | 76,880 | 244,908 | 35,528 | 4.0 | 32.7 | 12.0 | 8,999 | 6,392 |
| B | 3 | 1,073,444 | 199,321 | 825,087 | 49,036 | 9.3 | 93.7 | 12.0 | 9,010 | 6,431 |
| C | 3 | 1,892,488 | 461,591 | 1,373,166 | 57,730 | 15.0 | 111.7 | 12.0 | 19,012 | 7,702 |

Token classes of the session, mean per run (measured). Cache reads are billed at a fraction of the other input classes, so the token ratio between configurations is not their cost ratio.

| configuration | input | cache creation | cache read | output | cache read share | API cost (USD) |
|---|---|---|---|---|---|---|
| A | 73 | 71,647 | 250,069 | 35,528 | 70.0% | 0.5958 |
| B | 206 | 101,998 | 922,205 | 49,036 | 85.9% | 0.8778 |
| C | 253 | 150,670 | 1,683,834 | 57,730 | 89.0% | 1.1745 |

## 2. Prompt tokens by what was read

Mean per run, with the share of the configuration's prompt tokens. A block is counted once for every model call whose prompt contained it. Totals are measured; the split inside one increase is by character share.

| what | A | B | C |
|---|---|---|---|
| first prompt of the lead, read by each of its calls | 35,996 (11.2%) | 84,093 (8.2%) | 285,181 (15.5%) |
| first prompt of each subagent, read by each of its calls | 208,803 (64.9%) | 602,313 (58.8%) | 860,075 (46.9%) |
| benchbook check_tried | 0 (0.0%) | 0 (0.0%) | 78,100 (4.3%) |
| benchbook record_experiment | 0 (0.0%) | 0 (0.0%) | 79,327 (4.3%) |
| benchbook brief | 0 (0.0%) | 0 (0.0%) | 104,404 (5.7%) |
| benchbook claims | 0 (0.0%) | 0 (0.0%) | 9,326 (0.5%) |
| benchbook tasks | 0 (0.0%) | 0 (0.0%) | 228,393 (12.4%) |
| benchbook other | 0 (0.0%) | 0 (0.0%) | 43,623 (2.4%) |
| evaluation Bash | 35,821 (11.1%) | 146,485 (14.3%) | 85,349 (4.7%) |
| other Bash | 0 (0.0%) | 16,807 (1.6%) | 1,748 (0.1%) |
| file Read | 0 (0.0%) | 3,375 (0.3%) | 0 (0.0%) |
| file Write | 0 (0.0%) | 76,440 (7.5%) | 0 (0.0%) |
| subagent delegation | 10,380 (3.2%) | 25,178 (2.5%) | 7,596 (0.4%) |
| subagent report | 30,148 (9.4%) | 68,631 (6.7%) | 48,250 (2.6%) |
| model text and thinking | 640 (0.2%) | 1,086 (0.1%) | 3,386 (0.2%) |
| **all prompt tokens (measured)** | 321,789 | 1,024,409 | 1,834,757 |

## 3. Model calls by what triggered them

Mean per run: model calls, and the prompt tokens those calls were billed (measured). A call is triggered by the tool calls of the response before it; a response with several kinds of tool call splits its follow-up call evenly between them.

| trigger | A | B | C |
|---|---|---|---|
| benchbook check_tried | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 15.5 calls, 188,085 (10.3%) |
| benchbook record_experiment | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 18.1 calls, 270,002 (14.7%) |
| benchbook brief | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 9.7 calls, 115,600 (6.3%) |
| benchbook claims | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 4.0 calls, 136,468 (7.4%) |
| benchbook tasks | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 27.3 calls, 406,290 (22.1%) |
| benchbook other | 0.0 calls, 0 (0.0%) | 0.0 calls, 0 (0.0%) | 2.2 calls, 70,366 (3.8%) |
| evaluation Bash | 20.7 calls, 168,210 (52.3%) | 48.3 calls, 410,060 (40.0%) | 33.2 calls, 425,391 (23.2%) |
| other Bash | 0.0 calls, 0 (0.0%) | 3.5 calls, 74,585 (7.3%) | 0.7 calls, 15,665 (0.9%) |
| file Read | 0.0 calls, 0 (0.0%) | 1.3 calls, 36,619 (3.6%) | 0.0 calls, 0 (0.0%) |
| file Write | 0.0 calls, 0 (0.0%) | 33.9 calls, 343,212 (33.5%) | 0.0 calls, 0 (0.0%) |
| subagent delegation | 3.0 calls, 67,881 (21.1%) | 3.0 calls, 73,749 (7.2%) | 3.0 calls, 95,455 (5.2%) |
| start of an agent | 13.0 calls, 85,697 (26.6%) | 13.0 calls, 86,183 (8.4%) | 13.0 calls, 111,436 (6.1%) |

## 4. Context growth by block category

Mean per run. `estimated` is chars/4 of the blocks, `measured` is their share of the measured prompt increases, `unsplit` is the part of `measured` from increases with a single category.

| configuration | category | blocks | estimated tokens | measured tokens | unsplit | measured / estimated |
|---|---|---|---|---|---|---|
| A | evaluation Bash | 129.3 | 7,980 | 27,100 | 14,461 | 3.40 |
| A | subagent delegation | 12.0 | 2,542 | 5,126 | 0 | 2.02 |
| A | subagent report | 12.0 | 7,462 | 15,078 | 0 | 2.02 |
| A | model text and thinking | 15.7 | 197 | 461 | 0 | 2.34 |
| B | evaluation Bash | 147.3 | 8,883 | 29,484 | 14,172 | 3.32 |
| B | other Bash | 7.3 | 1,384 | 3,360 | 3,073 | 2.43 |
| B | file Read | 2.7 | 669 | 1,711 | 1,163 | 2.56 |
| B | file Write | 120.0 | 10,573 | 26,863 | 6,461 | 2.54 |
| B | subagent delegation | 12.0 | 2,741 | 5,363 | 0 | 1.96 |
| B | subagent report | 12.0 | 7,384 | 14,467 | 0 | 1.96 |
| B | model text and thinking | 22.7 | 93 | 224 | 0 | 2.41 |
| C | benchbook check_tried | 98.7 | 4,111 | 13,917 | 510 | 3.39 |
| C | benchbook record_experiment | 117.3 | 7,420 | 26,932 | 19,795 | 3.63 |
| C | benchbook brief | 26.7 | 5,885 | 13,902 | 5,804 | 2.36 |
| C | benchbook claims | 8.0 | 421 | 1,488 | 1,307 | 3.53 |
| C | benchbook tasks | 57.3 | 7,019 | 30,509 | 29,558 | 4.35 |
| C | benchbook other | 7.3 | 1,918 | 4,585 | 605 | 2.39 |
| C | evaluation Bash | 124.7 | 7,495 | 21,078 | 7,371 | 2.81 |
| C | other Bash | 2.7 | 41 | 128 | 0 | 3.09 |
| C | subagent delegation | 12.0 | 519 | 999 | 0 | 1.93 |
| C | subagent report | 12.0 | 3,126 | 6,017 | 0 | 1.92 |
| C | model text and thinking | 28.0 | 233 | 652 | 0 | 2.80 |

## 5. The lead's largest prompt

Mean per run: the first prompt plus the measured increases up to the largest prompt, split by character share.

| part | A | B | C |
|---|---|---|---|
| first prompt (measured) | 8,999 | 9,010 | 19,012 |
| benchbook brief | 0 | 0 | 1,278 |
| benchbook claims | 0 | 0 | 1,488 |
| benchbook tasks | 0 | 0 | 6,233 |
| benchbook other | 0 | 0 | 4,585 |
| evaluation Bash | 0 | 291 | 0 |
| other Bash | 0 | 3,099 | 128 |
| file Read | 0 | 1,711 | 0 |
| subagent delegation | 5,126 | 5,363 | 999 |
| subagent report | 15,078 | 14,467 | 6,017 |
| model text and thinking | 202 | 69 | 122 |
| **largest prompt (measured)** | 29,405 | 34,010 | 39,861 |

## 6. Repeated and nearby evaluations

From evals.jsonl (measured). Each evaluation is compared with the ones that finished before it in the same run. `near` counts evaluations with no earlier identical config but an earlier one with the same non-numeric fields and every numeric field within the tolerance.

| run | evaluations | duplicate ratio | exact repeat | replicate | near at 10% | near at 25% | near at 50% |
|---|---|---|---|---|---|---|---|
| sonnet55-ae81ddc-run2/A-1 | 60 | 55.0% | 0 | 27 | 0 | 1 | 7 |
| sonnet55-ae81ddc-run2/A-2 | 60 | 36.7% | 0 | 13 | 0 | 0 | 21 |
| sonnet55-ae81ddc-run2/A-3 | 60 | 30.0% | 1 | 12 | 0 | 0 | 21 |
| sonnet55-ae81ddc-run2/B-1 | 60 | 21.7% | 0 | 10 | 0 | 5 | 19 |
| sonnet55-ae81ddc-run2/B-2 | 60 | 16.7% | 0 | 6 | 0 | 7 | 27 |
| sonnet55-ae81ddc-run2/B-3 | 60 | 5.0% | 0 | 2 | 0 | 6 | 33 |
| sonnet55-ae81ddc-run2/C-1 | 59 | 32.2% | 0 | 12 | 0 | 0 | 16 |
| sonnet55-ae81ddc-run2/C-2 | 59 | 32.2% | 0 | 15 | 0 | 2 | 20 |
| sonnet55-ae81ddc-run2-C3/C-1 | 58 | 25.9% | 0 | 10 | 0 | 0 | 16 |

## Hypotheses (spec §10)

A verdict is computed from the stated measure: supported at 50% or more, contradicted below 20%, partly supported in between, undetermined when an input it needs is missing.

### Hypothesis 1: Recording and looking up spend agent turns: every attempt costs a separate check_tried and a separate record call.

**Verdict: partly supported** (prompt tokens of the model calls that follow check_tried or record_experiment, as a share of C's session-token gap over A: 29.8%).

- C made 49.3 check_tried and 58.7 record_experiment tool calls for 58.7 evaluations per run (measured). They were followed by 33.6 model calls, 3.2 tool calls per model call: the agents batch several lookups or records into one turn.
- Model calls triggered by check_tried or record_experiment: 33.6 of 126.7 per run (26.5%), billed 458,087 prompt tokens (24.2% of C's session tokens; measured).
- Model calls triggered by any benchbook tool: 76.8 per run (60.6%), billed 1,186,810 prompt tokens (62.7% of C's session tokens; measured).
- C used 1,535,171 more session tokens per run than A (1,892,488 against 357,316; measured). The calls that follow a lookup or a record account for 29.8% of that gap, the calls that follow any benchbook tool for 77.3%.
- Model calls per run: C 126.7, A 36.7. First prompt of a subagent: C 7,702, A 6,392 tokens (measured), read again by every one of its calls.

### Hypothesis 2: The exact hash misses nearby configurations; agents that read result files avoid similar configurations by eye.

**Verdict: contradicted** (near configs (within 25%) as a share of C's repeated or nearly repeated evaluations, set to zero when B has at least as many near configs as C: 0.0%).

- C per run: 0.0 exact repeats (same config, code and seed), 12.3 replicates (same config and code, a new seed) and 0.7 evaluations within 25% of an earlier config on every numeric field, out of 58.7 (measured from evals.jsonl).
- Of C's evaluations that repeat or nearly repeat an earlier one, 5.1% are near configs, which an exact hash cannot see; the rest have an identical config, which it does see.
- The benchmark's duplicate ratio (30.1% in C) counts an evaluation when its (config, code) occurs twice, whatever the seed: replicates raise it and near configs never do.
- Near configs per run at 25%: B 6.0, C 0.7, A 0.3. B does not have fewer, so reading the result files did not make agents avoid them.
- Near configs per run by tolerance: 10%: A 0.0, B 0.0, C 0.0; 25%: A 0.3, B 6.0, C 0.7; 50%: A 16.3, B 26.3, C 17.3.

### Hypothesis 3: The brief grows the lead's context.

**Verdict: contradicted** (tokens of brief calls and results in the lead's largest prompt, as a share of the gap between C's and A's largest lead prompt: 12.2%).

- C's largest lead prompt: 39,861 tokens per run (measured), of which 19,012 were there at the first call (system prompt, tool definitions, the lead skill and the task).
- Of the growth after that, brief calls and results account for 1,278 tokens, all benchbook tool calls and results for 13,584, subagent reports for 6,017 (measured increases, split inside an increase by character share).
- C's largest lead prompt exceeds A's by 10,456 tokens (29,405 in A). The brief accounts for 12.2% of that gap, all benchbook tool calls and results for 129.9%, and the larger first prompt (+10,013 tokens) for 95.8%. The parts need not add up to the gap: A's lead prompt holds 15,078 tokens of subagent reports against C's 6,017.

## What these numbers do not cover

- System prompt, tool definitions, skill text and system reminders are not in the stream. They are inside each agent's first prompt (measured as a whole) and inside the increases, where they are split over the visible blocks or, with none, counted as `not in the stream`.
- Thinking is forwarded without its text, so its tokens are split over the visible blocks of the same increase.
- Output tokens are reported per session and per lead only. Blocks written after an agent's last model call (a subagent's closing report, the lead's structured output) were never part of a prompt and are left out of the tables; they come to A 10,034, B 7,274, C 1,352 estimated tokens per run.
