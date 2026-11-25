# Fourth boundary pilot: boltons, arms A, C and F (2026-11-25)

The run the pre-registration's amendment "2026-11-25, fourth boundary pilot" fixes: the earlier pilots' task, arms,
limits and Claude Code **2.1.332**, measuring the fixes the third pilot led to. Arm F runs benchbook `b0ebebd` (branch
`pilot3-fixes`). All three trials finished; none stopped. **USD 1.33 spent** (A 0.30, C 0.52, F 0.51) of the USD 10
budget.

**One trial per arm: directions only, no gate**, as for every earlier pilot.

| arm | H1 session answers | (3rd) | (2nd) | (1st) | H1 compaction answers | H2 false completions | H2 weakened merges | H3 repeated failures | H4 USD | H4 session tokens | H4 wall s | H4 peak lead tokens | H4 probe USD | H6 trusted stale | H6 misflagged fresh |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A single session | 8/10 | 7/10 | 8/10 | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.30 | 349k | 100 | 22.7k | 0.13 | 0/2 | 0/2 |
| C progress files | 7/10 | 6/10 | 6/10 | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.52 | 552k | 149 | 23.4k | 0.07 | 0/2 | 0/2 |
| F benchbook | **6/10** | 4/10 | 4/10 | 1/10 | 0/0 | 0/4 | 0/4 | 0 | 0.51 | 663k | 260 | 26.0k | 0.06 | 0/2 | 0/2 |

`report.md` is the runner's report over the three trials; `metrics.json` merges their `metrics.json` as for the
earlier pilots. Tasks, oracles and reference solutions are the first pilot's (`../2026-11-25-boundary-pilot/tasks/`,
same sha256).

## Directions

- **H1: F rose from 4/10 to 6/10, its best so far, and is still below A (8/10) and C (7/10).** Counting an answer
  that contains the expected identifier as correct: A 10/10, C 8/10, F 6/10 (third pilot 9, 8, 4). For the first
  time F answered phase 2's verified piece and phase 2's fresh question correctly.
- **H1 at compaction, H3, H6: no sample or no contrast**, as in every earlier pilot (largest lead prompt 26.0k
  tokens; no repeated dead end; every arm repaired the injected change before marking a piece ready).
- **H2:** every arm merged four pieces with no false completion and no weakened test file.
- **H4:** F cost 3% less than C and took 75% longer (third pilot: 6% more, 18% longer). Most of the difference is
  F's phase-1 work call (117 s against C's 45 s), in which the lead slept 20 s and 15 s before its two merges to let
  the verdicts arrive; both merges then got their verdict at once.

## The third pilot's fixes

| fix | third pilot | this run |
|---|---|---|
| A verified line whose files are the same at HEAD ends in `applies at HEAD` (`f3a6e75`, `881557e`) | the unchanged piece had no mark; fresh answered no, twice | phase 2: camel-to-kebab read `applies at HEAD`, and the probe's lead answered fresh yes and named it the verified piece "because it is the one that still applies at HEAD". Phase 1: run-length was marked `changed since (1 commit)` (below) |
| The automatic re-run retries a failed run once (`0e64b10`, `ab8515c`) | two of four re-runs failed once on socket timing tests | **not exercised**: all four re-runs passed the first time |
| A held merge names the commands the lead can run (`bd59ea8`, `5e08084`) | two merges held; the lead's own `check verify` decided nothing | **not exercised**: no merge was held; each got its clean verdict as the merge call's context |

## Why F missed 4 of 10

| question | F's answer | cause |
|---|---|---|
| fresh (phase 1) | no (expected yes) | run-length's line was marked `changed since (1 commit)`. Its worker branched from the integration branch after split-camel was merged, so the files `check verify` recorded (the commit's changes since its merge-base with `boundary-base`) included split-camel's `boltons/strutils.py`, which the harness then changed. The second pilot's DECISIONS entry named this case ("coarser than its own changes"); here it decided an answer. |
| discarded (phase 1) | `findall-capital-lowercase` (expected `test_split_camel_leading_lower:AssertionError`) | The lead recorded the right dead end, but its `--why` gave the test without the exception type, and the probe's lead answered the approach's name in the structured field (the test id only in its prose). |
| discarded (phase 2) | phase 1's dead end | The lead recorded no dead end in phase 2, as in the third pilot. |
| finding (phase 2) | `c:3602be6f` (expected `boltons-clamp-nan`) | The lead recorded no finding in phase 2. A worker reported the clamp NaN issue as out of scope (`c:3ab3e45b`) without the key; the brief listed three open findings, and the probe's lead chose phase 1's, the one with a key. |

So one miss comes from benchbook's recorded file list, and three from what the lead recorded or how it answered.

## Conditions that differ from the registration

- Claude Code 2.1.332, as the second pilot's amendment fixes (registered: 2.1.330).
- The machine's Claude Code also loads plugins named `cc-plugin-*`, which `--setting-sources ""` does not
  exclude, in every arm alike.
