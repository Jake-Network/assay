# H5 fan-in sweep: circle packing at 8, 16 and 30 workers (2026-11-25)

The H5 sweep of `bench/boundary/PREREGISTRATION.md` (`**H5 budget**` and the Amendments dated 2026-11-23 and
2026-11-24). Circle packing (n=26), configuration A (subagents report as text to the lead) against C (benchbook),
`claude-sonnet-5-5`, USD 5 per run, benchbook code `cc5ed08` (tag `bench-h5-code`) for every run. Registered: 3 runs
per configuration and width; the lean amendment of 2026-11-24 cut width 30 to at most 2 runs each. **USD 52.48 spent**
over 21 runs, of which USD 10.08 went to 5 runs stopped by the account's five-hour usage limit.

## Gate: not met

Pre-registered gate: *Circle-packing C lower than A at each width and grows more slowly*, on peak lead context and on
the estimated tokens of the Agent/Task results the lead reads (lead report tokens).

| width | config | completed runs | delivered reports | peak lead context (tokens) | lead report tokens (estimate) | USD per run |
|---|---|---|---|---|---|---|
| 8 | A | 3 | 24 / 24 / 26 | 44.4k / 47.3k / 49.7k (mean 47.2k) | 14.2k / 16.0k / 16.0k (mean 15.4k) | 1.09–1.18 |
| 8 | C | 3 | 24 / 24 / 24 | 55.1k / 54.8k / 55.6k (mean 55.2k) | 6.9k / 6.7k / 7.0k (mean 6.9k) | 2.08–2.33 |
| 16 | A | 3 | 48 / 48 / 48 | 81.9k / 86.0k / 82.7k (mean 83.5k) | 30.9k / 31.5k / 30.9k (mean 31.1k) | 2.16–2.37 |
| 16 | C | 3 | 48 / 48 / 46 | 78.9k / 91.2k / 87.6k (mean 85.9k) | 13.9k / 13.3k / 14.0k (mean 13.8k) | 4.18–4.29 |
| 30 | A | 2 | 89 / 86 | 159.6k / 142.0k (mean 150.8k) | 62.2k / 56.0k (mean 59.1k) | 4.10 / 4.01 |
| 30 | C | 2, both off-condition (below) | 90 / **28** | 130.7k / 53.8k | 22.3k / 8.0k | 1.91 / 2.70 |

Delivered reports are the lead's successful Agent/Task results; the request was workers × 3 rounds (24, 48, 90).

- **Peak lead context: not met.** C is above A at width 8 in all three pairs (55.2k vs 47.2k on the means) and at
  width 16 on the mean (85.9k vs 83.5k; C above A in 2 of 3 pairs). The gate fails at width 8 whatever width 30 shows.
  From 8 to 16 C grew less than A (+30.7k vs +36.3k).
- **Lead report tokens: C lower at every width, and grows more slowly.** C's reports to the lead are 45% of A's at 8
  and 44% at 16 (8→16: A +15.7k, C +6.9k). At 30 both C runs are again far below A, but neither is a like-for-like
  sample (below).
- The two metrics point in opposite directions. C's lead reads less from its workers, but it also works with the
  ledger itself (17.7 lead model calls per run against A's 4.0 at width 8, 19.3 against 4.0 at 16,
  `cost-breakdown.md` section 1), and its peak is not lower. This matches the earlier result in the README (peak
  lead context below A: **fail**).
- Width 30 has 2 runs per configuration, short of the 3 the pre-registration fixes as the minimum sample, so no gate
  could be claimed there in any case.

## Width 30: neither C run is a like-for-like sample

Both C runs finished under the USD 5 cap, so the lean amendment's second pair ran. In both the lead changed the
workload to fit the cap, which the prompt leaves to it:

- **`30-C-1` ran its workers on Haiku.** The lead passed `model: "haiku"` to all 100 of its Agent calls, and
  1,851 subagent messages came from `claude-haiku-5-5` (USD 0.51 of the run's 1.91). The registered model is
  `claude-sonnet-5-5` for lead and subagents. Its lead is still Sonnet and delivered 90 reports, so its peak lead
  context (130.7k) is reported, but its report tokens come from a different worker model.
- **`30-C-2` ran 28 workers instead of 90** (rounds of 13, 9 and 6). Its final summary says each worker cost about
  USD 0.09, so 90 would have passed USD 5. With 28 reports its peak lead context (53.8k) is that of a width-9 run.
- The A runs at 30 used Sonnet workers and delivered 89 and 86 of 90 reports.

Cost is the reason: at width 16, C already cost USD 4.18–4.29 per run against A's 2.16–2.37, so a C run with 90
Sonnet workers does not fit the registered USD 5 cap. Counting the lead and its subagents, C used 5.5x (8) and 4.8x (16) the
session tokens of A and 2.0x and 1.9x its API cost (`workers-*/cost-breakdown.md`).

## Conditions that differ from the registration

- **Claude Code version.** Widths 8 and 16 ran on 2.1.330 as registered. Every width-30 run, including the stopped
  `30-A-2-try1`, ran on **2.1.331**: the installed CLI was updated to 2.1.331 on 2026-11-24 at about 07:03 KST, while
  the remaining runs were going, and the driver did not check the version. A and C at width 30 ran on the same version, so the
  width-30 pairs are matched with each other; the growth from 16 to 30 compares two CLI versions. Each run's version is
  in the `system/init` event of its stream.
- **Run layout.** After the second usage-limit stop, the remaining runs ran one `bench.run --runs 1` each from a
  detached worktree at `cc5ed08`; widths were rebuilt from the run directories with `bench.real.parse` unchanged
  (amendments of 2026-11-23 and 2026-11-24).
- **Width 30 cut to 2 runs per configuration** (lean amendment of 2026-11-24, written before the width-30 runs).

## Stopped runs

All five stopped on the account's five-hour usage limit (HTTP 429, "You've hit your 5-hour limit"). None reached the
USD 5 cap. Each is listed in its width's `metrics.json` under `conditions.failed` and was repeated after the reset.

| run | width | config | USD | minutes |
|---|---|---|---|---|
| `h5-sweep/workers-8/C-3` | 8 | C | 1.98 | 5.0 |
| `h5-sweep-16-30/workers-16/C-1` | 16 | C | 1.09 | 2.1 |
| `h5-runs/16-C-2-try1` | 16 | C | 2.34 | 4.2 |
| `h5-runs/16-C-3-try1` | 16 | C | 2.73 | 4.5 |
| `h5-runs/30-A-2-try1` | 30 | A | 1.95 | 4.2 |

## Other numbers

- Every completed run reached the same best value (2.636), and no run lost a result (`lost results` 0).
- Duplicate ratio, A vs C: 0.36 vs 0.37 (8), 0.53 vs 0.50 (16), 0.75 vs 0.44 (30, C off-condition).
- Wall time per completed run: A 4.9–6.8 min (8), 11.3–11.6 (16), 16.4–18.4 (30); C 5.3–5.7 (8), 7.5–8.5 (16),
  12.0 and 5.9 (30).

## Files

Per width, `workers-N/` holds `report.md` and `curve.png`
(`python -m bench.report`), `metrics.json` (rebuilt with `bench.real.parse`), and `cost-breakdown.md`
(`python -m bench.analyze_cost` over the completed runs).

## Where the runs came from

Raw output (streams, transcripts, ledgers) stays outside the repository, in the bench directory that the paths in
`metrics.json` are relative to. Each width's `metrics.json` was rebuilt from these run directories with
`bench.real.parse` unchanged, because `bench.run` writes one only when a whole configuration group ends. A
`:<label>` after a path renumbers a run whose directory name repeats another's.

| width | config | completed runs | stopped runs (usage limit) | Claude Code |
|---|---|---|---|---|
| 8 | A | `h5-sweep/workers-8/A-{1,2,3}` | | 2.1.330 |
| 8 | C | `h5-sweep/workers-8/C-{1,2}`, `h5-sweep-c8r/C-1` (as C-4) | `h5-sweep/workers-8/C-3` | 2.1.330 |
| 16 | A | `h5-sweep-16-30/workers-16/A-{1,2,3}` | | 2.1.330 |
| 16 | C | `h5-runs/16-C-1-try1/C-1` (C-2), `h5-runs/16-C-2-try2/C-1` (C-4), `h5-runs/16-C-3-try2/C-1` (C-6) | `h5-sweep-16-30/workers-16/C-1`, `h5-runs/16-C-2-try1` (C-3), `h5-runs/16-C-3-try1` (C-5) | 2.1.330 |
| 30 | A | `h5-runs/30-A-1-try1/A-1` (A-1), `h5-runs/30-A-2-try2/A-1` (A-3) | `h5-runs/30-A-2-try1` (A-2) | 2.1.331 |
| 30 | C | `h5-runs/30-C-1-try1/C-1` (C-1, Haiku workers), `h5-runs/30-C-2-try1/C-1` (C-2, 28 workers) | | 2.1.331 |

`h5-sweep` and `h5-sweep-16-30` ran `bench.run` over whole groups from the branch on 2026-11-23, whose code
(`src`, `bench`, `examples`, `integrations`) had last changed in `c37b2b9` and is the same at `cc5ed08`; `h5-runs` ran
one `bench.run --runs 1` per run from a detached worktree at `cc5ed08` (tag `bench-h5-code`), built with
`SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+h5code`.
