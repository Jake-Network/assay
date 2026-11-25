# Second boundary pilot: boltons, arms A, C and F (2026-11-25)

The run the pre-registration's amendment "2026-11-25, second boundary pilot" fixes: the first pilot's task (boltons),
arms A, C and F, one repetition each, `claude-sonnet-5-5` for the lead and subagents, Claude Code **2.1.332** (checked
by the runner before any paid call), USD 3 per work call and 0.5 per probe, auto-compact window 100,000. Arm F runs
benchbook `4ba17cf` (branch `lead-guards`), which fixes what the first pilot found (below). All three trials
finished; none stopped. **USD 1.28 spent** (A 0.28, C 0.53, F 0.47) of the USD 10 budget.

**One trial per arm: directions only, no gate**, as for the first pilot.

| arm | H1 session answers | (first pilot) | H1 compaction answers | H2 false completions | H2 weakened merges | H3 repeated failures | H4 USD | H4 session tokens | H4 wall s | H4 peak lead tokens | H4 probe USD | H6 trusted stale | H6 misflagged fresh |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A single session | 8/10 | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.28 | 296k | 103 | 21.9k | 0.11 | 0/2 | 0/2 |
| C progress files | 6/10 | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.53 | 606k | 194 | 23.1k | 0.07 | 0/2 | 0/2 |
| F benchbook | **4/10** | 1/10 | 0/0 | 0/4 | 0/4 | 0 | 0.47 | 667k | 227 | 24.9k | 0.04 | 0/2 | 0/2 |

`report.md` is the runner's report over the three trials; `metrics.json` merges their `metrics.json` as for the first
pilot. Tasks, oracles and reference solutions are the first pilot's (`../2026-11-25-boundary-pilot/tasks/`, same
sha256).

## Directions

- **H1: F rose from 1/10 to 4/10 and is still below A (8/10) and C (6/10).** Counting an answer that contains the
  expected identifier as correct (the first pilot's sensitivity): A 10/10, C 7/10, F 4/10 (first pilot 8, 7, 1).
- **H1 at compaction, H3, H6: no sample or no contrast**, as in the first pilot: the largest lead prompt was 24.9k
  tokens, no arm repeated a registered dead end, and every arm repaired the injected change before marking a piece
  ready.
- **H2:** every arm merged four pieces with no false completion and no weakened test file.
- **H4:** A was again the cheapest and fastest; F cost 11% less than C and took 17% longer.

## What changed in F, and what it did

| fix (since the first pilot's `937f5ea`) | first pilot | this run |
|---|---|---|
| Adding a name to a Python test import is not a weakening (PR #8) | 3 of 4 pieces "needs review", 1 verified | 4 of 4 verified, "tests not weakened" |
| A lead's `git merge` of a reported commit waits for its verdict (`lead-guards`) | every merge before its verdict | every merge got its clean verdict as the merge call's context; no merge was held |
| One "re-running" line per report (PR #8) | two per report | one per report |
| Session-start guide for the lead, `benchbook discard`, `benchbook finding add` (`lead-guards`) | no worker asked for findings; no dead end or finding recorded | every worker prompt asked for `DONE <sha>` and an `## Out of scope` section, and every report had both; in phase 2 the lead recorded the dead end and the finding itself |
| The runner copies each call's transcripts (PR #9) | read from `~/.claude/projects` | in each call's `transcripts/` |

## Why F missed 6 of 10

| question | F's answers (phase 1, phase 2) | cause |
|---|---|---|
| fresh | no, no (expected yes, yes) | Every verified line carried `changed since (N commits)`. Without `piece start` a piece's paths are the directory prefixes its commit changed (`boltons/`, `tests/`), so any later commit under them, including the other piece's merge, marks it changed. |
| verified (phase 2) | weighted-median (expected camel-to-kebab) | The same marks: all four pieces looked changed, and the lead named the newest. |
| discarded (phase 2) | the phase-1 action's failing check run (expected the camel-to-kebab dead end) | The lead had recorded the right dead end with `benchbook discard`, but the claim carries only `piece:boltons-p2-camel-to-kebab`, which is outside the `--here` scope of the integration branch (`piece:boundary-work` and `file:` prefixes); the brief showed only the discarded approach left by the lead's own check run against the harness's injected change. |
| discarded, finding (phase 1) | unknown, unknown | The lead recorded neither in phase 1 (it did in phase 2). The registered finding is stated in the task prompt only, so no worker could report it. A worker did report a real one (a failing doctest, c:0dfb18d1), which the restart brief did not show for the same scope reason. |

So three misses come from the validity marks, two from the brief's scope on the integration branch, and two (one of
them overlapping) from what the lead chose to record.

## Conditions that differ from the registration

- Claude Code 2.1.332, as the amendment fixes (registered: 2.1.330; the first pilot ran 2.1.331).
- The machine's Claude Code also loads plugins named `cc-plugin-*`, which `--setting-sources ""` does not exclude,
  in every arm alike (amendment).
