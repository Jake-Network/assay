# Third boundary pilot: boltons, arms A, C and F (2026-11-25)

The run the pre-registration's amendments "2026-11-25, third boundary pilot" and "... A's first trial stopped on a
receipt" fix: the second pilot's task, arms, limits and Claude Code **2.1.332**, measuring the two fixes the second
pilot led to. Arm F runs benchbook `345b193` (branch `boundary-pilot-3`; its benchbook code is the same as at
`c9eb56e`). All three counted trials finished. **USD 1.88 counted** of the USD 10 budget: A 0.25, C 0.52, F 0.55,
and 0.56 for A's stopped first trial (below).

**One trial per arm: directions only, no gate**, as for both earlier pilots.

| arm | H1 session answers | (2nd pilot) | (1st pilot) | H1 compaction answers | H2 false completions | H2 weakened merges | H3 repeated failures | H4 USD | H4 session tokens | H4 wall s | H4 peak lead tokens | H4 probe USD | H6 trusted stale | H6 misflagged fresh |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A single session | 7/10 | 8/10 | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.25 | 273k | 94 | 21.6k | 0.09 | 0/2 | 0/2 |
| C progress files | 6/10 | 6/10 | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.52 | 625k | 201 | 22.6k | 0.06 | 0/2 | 0/2 |
| F benchbook | **4/10** | 4/10 | 1/10 | 0/0 | 0/4 | 0/4 | 0 | 0.55 | 891k | 237 | 27.4k | 0.05 | 0/2 | 0/2 |

`report.md` is the runner's report over the three counted trials; `metrics.json` merges their `metrics.json` as
for the earlier pilots. Tasks, oracles and reference solutions are the first pilot's
(`../2026-11-25-boundary-pilot/tasks/`, same sha256).

## A's first trial, stopped

`boundary-pilot3-boltons-A-try1` stopped at its second call, the phase-1 resume probe: that resume reported only its
own spend (USD 0.0623 after the work call's 0.0602), as Claude Code had not restored the session's earlier totals,
and the runner stopped on "a resumed session reported less spend than its previous receipt". It counts USD 0.56
(0.06 plus the probe's full USD 0.5 cap). The runner now reads a call's own spend from the result event's
`model_usage_since_last_result` (`e592049`); replaying the second pilot's A and F streams through it gives every
call's recorded cost and tokens exactly. A ran again as `try2`, whose resumes did restore the totals. After the run a
review added a check that a call's own usage fits its receipt (the receipt's totals are the own usage, or the previous
receipt's totals plus it; commit `5931db1`); all 37 calls of the second and third pilots pass it with the same numbers,
and so does A-try1's stopped resume.

## Directions

- **H1: F stayed at 4/10, below A (7/10) and C (6/10).** Counting an answer that contains the expected identifier as
  correct: A 9/10, C 8/10, F 4/10 (second pilot 10, 7, 4; first pilot 8, 7, 1). F got four answers right in both
  runs, but not the same four: this time it answered phase 1's dead end and missed phase 2's verified piece.
- **H1 at compaction, H3, H6: no sample or no contrast**, as in both earlier pilots: the largest lead prompt was 27.4k
  tokens, no arm repeated a registered dead end, and every arm repaired the injected change before marking a piece
  ready.
- **H2:** every arm merged four pieces with no false completion and no weakened test file.
- **H4:** A was again the cheapest and fastest; F cost 6% more than C and took 18% longer (second pilot: 11% less,
  17% longer). F's session tokens rose from 667k to 891k; 201k of the 224k more are in the two work calls, where the lead looked into the two failed re-runs.

## The two fixes

| fix | second pilot | this run |
|---|---|---|
| "Changed since" compares the files the verified commit changed (`5e9ded6`) | every verified line marked `changed since`, including the unchanged control piece | run-length, whose files nothing changed, had no mark in either phase; weighted-median, which the harness changed in phase 2, was marked `changed since (2 commits)` |
| A checkout's brief covers the pieces merged into HEAD (`0d9318f`) | the lead's `benchbook discard` and a worker's finding missing from the integration branch's brief | both in the brief (Scope: `+ 2 pieces merged into HEAD`, then `+ 4`); phase 1's dead end answered correctly |

Both did what they were for. F's score did not move because of what the brief did not say and what the lead did not
record, below.

## Why F missed 6 of 10

| question | F's answers (phase 1, phase 2) | cause |
|---|---|---|
| fresh | no, no (expected yes, yes) | Phase 1: run-length's line had no "changed since" mark, but the probe's lead said no because HEAD had moved to the harness commit, whose message reads "change a checked file". The brief marks a changed piece; it says nothing for an unchanged one, and the absence was not read as "still applies". Phase 2: camel-to-kebab was not verified at all (next row). |
| verified (phase 2) | weighted-median (expected camel-to-kebab) | camel-to-kebab's independent re-run failed on a timing test outside the piece, `tests/test_socketutils.py::test_socketutils_netstring` (Timeout). The lead's own three suite runs and its own `benchbook check verify` passed, but that verify ran without `--reported-by` and as `verifier@<host>`, so it decided nothing ("no completion claim to decide"). The piece stayed under "Re-run, not decided" as "passed, not decided". The same happened to phase 1's split-camel (`test_netstring_read_ns_timeout_covers_payload`), which was not asked about. |
| discarded (phase 2) | the socketutils Timeout (expected the camel-to-kebab dead end) | The lead recorded no dead end in phase 2 (it did in phase 1). The failed re-run itself was listed first under "Discarded approaches" as "(no approach named) on boltons-p2-camel-to-kebab". |
| finding (phase 1) | `c:4e77ca90` (expected `boltons-under2camel-digits`) | The lead recorded the finding with `benchbook finding add` and the brief showed it with the key in its text; the probe's lead answered with the record id and named the key only in its prose. |
| finding (phase 2) | `c:4e77ca90` (expected `boltons-clamp-nan`) | The lead recorded no finding in phase 2, so the brief still listed phase 1's. |

## Verdicts and the merge gate (F, from the copied transcripts)

| phase | piece | re-run verdict | what reached the lead | merge |
|---|---|---|---|---|
| 1 | split-camel | failed (socketutils timing test) | as the deny reason of the lead's `git merge` | held once; the lead ran `benchbook check verify` (passed, no decision) and merged again |
| 1 | run-length | passed, verified | after the lead's next Bash call | merged, not held |
| 2 | weighted-median | passed, verified | as the context of the lead's `git merge`, which waited 2.3 s for it | merged, not held |
| 2 | camel-to-kebab | failed (socketutils timing test) | as the deny reason of the lead's `git merge` | held once; the lead ran the suite three times and `benchbook check verify` (passed, no decision), then merged again |

The gate held both failed verdicts as designed, and the lead read each reason and checked before merging. What it
could not do was turn its own passing re-run into a decision: the held message offers `benchbook decide ... --as
human` or merging again, and the lead's `check verify` without `--reported-by` records a run but no decision.
Both failed tests are socket tests with timeouts; every suite run and verify the lead ran on the pieces passed, and in
the second pilot all four re-runs passed on the same machine.

## Conditions that differ from the registration

- Claude Code 2.1.332, as the second pilot's amendment fixes (registered: 2.1.330).
- The machine's Claude Code also loads plugins named `cc-plugin-*`, which `--setting-sources ""` does not
  exclude, in every arm alike.
- A's counted trial is its second (`try2`); the first stopped on a receipt the runner could not read (above).
