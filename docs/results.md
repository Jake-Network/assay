# Results

What has been measured, with links to the runs. Every number here was produced by the harnesses under `bench/`;
`python -m bench.run --dry` reproduces the pipeline without model calls.

## Benchmark: experiment swarms

<!-- Report: bench/results/2026-11-21-claude-sonnet-5-5-rerun/report.md (bench at d65441a, claude-sonnet-5-5,
     Claude Code 2.1.330, 3 runs per arm, USD 5 cap per run). The first batch is in
     bench/results/2026-11-20-claude-sonnet-5-5/; the rerun's README says what differs. -->

Circle packing (n=26) with the same model, API budget, worker count, and wall-clock limit (mean ± sd over 3 runs):

| | lost results | duplicate evaluations | lead context (max) | best per 1k tokens |
|---|---|---|---|---|
| A: subagents report as text | 0 | 29.4% ± 15 | 30.5k ± 1.3k | 0.0281 ± 0.0012 |
| B: subagents write JSON files | 0.33 ± 0.58 | 16.1% ± 5.4 | 35.6k ± 3.1k | 0.0139 ± 0.0011 |
| C: benchbook | 0 | 22.5% ± 10 | 33.1k ± 2.0k | 0.0085 ± 0.0020 |

Release gate (MANUAL_QA.md), C against A:

- Lost results 0: **pass** (0 in every C run; A also lost nothing, B lost one evaluation in one run).
- Duplicate ratio below A: **pass** on the means (22.5% vs 29.4%, within one standard deviation at three runs; B's
  result files did better at 16.1%).
- Peak lead context below A: **fail** (33.1k vs 30.5k tokens).

All arms reached the same best (2.636). Counting the lead and its subagents, C used 4.2x the tokens of A and 1.8x
its API cost (B: 3.2x and 1.5x). Where C's tokens go is broken down in
[cost-breakdown.md](../bench/results/2026-11-21-claude-sonnet-5-5-rerun/cost-breakdown.md): mostly more model calls,
each re-reading its context, rather than large tool results.

**Fan-in (H5, 2026-11-25).** The same task at 8, 16 and 30 requested workers per round, A against C, USD 5 cap per
run (mean over completed runs: 3 per configuration at 8 and 16; at 30, 2 for A and 1 for C):

| workers | lead context (max), A | lead context (max), C | report tokens to the lead, A | report tokens to the lead, C |
|---|---|---|---|---|
| 8 | 47.2k | 55.2k | 15.4k | 6.9k |
| 16 | 83.5k | 85.9k | 31.1k | 13.8k |
| 30 | 150.8k | 130.7k (Haiku workers) | 59.1k | 22.3k (Haiku workers) |

Pre-registered H5 gate, C lower than A at every width and growing more slowly: **not met**. C's lead reads less than
half of A's report tokens, but its peak context is higher at 8 and 16. At 30 neither C run is a like-for-like sample:
to stay under USD 5, one lead ran its workers on Haiku and the other ran 28 of 90 workers. Width 30 ran on Claude Code
2.1.331. Details: [bench/results/2026-11-25-h5-sweep/](../bench/results/2026-11-25-h5-sweep/README.md).

## Coding work

**How often "done" is not done (SWE-bench Verified, 2026-11-23):** 7 of 39 completions that Claude Code
(`claude-sonnet-5-5`) reported done failed the benchmark's declared checks (visible and hidden tests, the registered
measure) when benchbook re-ran them: **17.9%, 95% CI 9.0% to 32.7%**
(the hidden tests alone: 5 of 39, 12.8%). Only 3 of the 39 implementers ran a test before reporting done. The interval
contains the 10% threshold of the plan; the point estimate is above it.
Post hoc, re-running only the visible checks a project declares, which is what benchbook does in real use, refuted
3 of 39 (7.7%, 2.7% to 20.3%); in two of those the reference fix fails the same visible test, which leaves 1 of 39 (2.6%,
0.5% to 13.2%). The four completions that only the hidden tests refuted need a better check, not a re-run.
Details, including two refutations caused by
the visible check itself: [bench/results/2026-11-23-swe-verified/](../bench/results/2026-11-23-swe-verified/README.md).
`bench/code/` is the harness. `bench/boundary/` measures what a lead still knows after compaction and restarts, across
six arms ([PREREGISTRATION.md](../bench/boundary/PREREGISTRATION.md)).

**What a lead still knows after a restart (four boundary pilots, 2026-11-25):** one task (boltons) and one trial per
arm in each pilot, so directions only. Asked five state questions after each of two phases (in a new session for F
and C, in the resumed session for A), benchbook's boundary mode (F) answered 1, 4, 4 and 6 of 10 across the four
pilots, a single resumed session (A) 6, 8, 7 and 8, and progress files (C) 6, 6, 6 and 7. Each pilot fixed what the
one before found in benchbook (a test import edit counted as a weakening, validity marks, the brief's scope on an
integration branch, the `applies at HEAD` mark); F has not been above progress files in any of them. No pilot reached compaction (largest lead prompt 27.4k tokens of
a 100k window), so compaction is still unmeasured; repeated dead ends (H3) and trust in the stale check (H6) were
counted and were 0 in every arm, so they show no difference yet. Per completed trial F cost about the same as C (USD
0.42 to 0.55 against 0.52 to 0.58) and 1.6 to 2.2 times A (0.25 to 0.30); the third pilot also spent USD 0.56 on an
A trial that stopped. Details: pilots
[1](../bench/results/2026-11-25-boundary-pilot/README.md), [2](../bench/results/2026-11-25-boundary-pilot-2/README.md),
[3](../bench/results/2026-11-25-boundary-pilot-3/README.md), [4](../bench/results/2026-11-25-boundary-pilot-4/README.md).

## Next

What comes next is decided by measurements, not features: the false completion rate in real delegated work, and a
boundary pilot that reaches compaction ([bench/boundary/PREREGISTRATION.md](../bench/boundary/PREREGISTRATION.md)).
Semantic near-duplicate warnings, row-level security for teams and new domain packs wait for a measured need.