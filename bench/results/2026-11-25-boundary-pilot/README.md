# Boundary pilot: boltons, arms A, C and F (2026-11-25)

The first paid run of `bench/boundary/PREREGISTRATION.md`, cut down by its amendments of 2026-11-24 to a pilot: one
self-built two-phase task (boltons), arms A (single resumed session), C (progress files) and F (benchbook boundary
mode), one repetition each. `claude-sonnet-5-5` for the lead and subagents, USD 3 per work call and 0.5 per probe,
auto-compact window 100,000, benchbook `937f5ea` for arm F. All three trials finished; none stopped. **USD 1.28
spent** (A 0.27, C 0.58, F 0.42) of the USD 30 pilot budget. toolz is registered but was not run (lean amendment).

**This is one trial per arm. The report gives directions only and claims no gate**, as the scope amendment fixes.

| arm | H1 session answers | H1 compaction answers | H2 false completions | H2 weakened merges | H3 repeated failures | H4 USD | H4 session tokens | H4 wall s | H4 peak lead tokens | H4 probe USD | H6 trusted stale | H6 misflagged fresh |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A single session | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.27 | 307k | 138 | 21.5k | 0.11 | 0/2 | 0/2 |
| C progress files | 6/10 | 0/0 | 0/4 | 0/4 | 0 | 0.58 | 694k | 296 | 22.5k | 0.07 | 0/2 | 0/2 |
| F benchbook | **1/10** | 0/0 | 0/4 | 0/4 | 0 | 0.42 | 560k | 297 | 24.2k | 0.03 | 0/2 | 0/2 |

`report.md` is the runner's report over the three trials (`python -m bench.boundary.report` on their merged
`metrics.json`).

## Directions

- **H1, F above C: the opposite.** F answered 1 of 10 state questions after a session restart; A and C answered 6.
  Why is below.
- **H1 at compaction: no sample.** No call reached the auto-compact window: the largest lead prompt was 24.2k tokens
  against 100k, so no `compact_boundary` event occurred and every compaction column is 0/0.
- **H3, F at least 30% below A: no information.** No arm repeated a registered dead end (0, 0, 0).
- **H6, F below C: no contrast.** After the injected change broke the stale piece, every arm repaired it before
  marking it `ready` (F's phase-1 action reverted the injection commit), so no arm trusted a stale check and none
  flagged the unchanged piece.
- **H2.** Every arm merged both pieces of both phases (4 of 4) with no false completion and no weakened test file by the
  oracle's rule.
- **H4.** A, one resumed session, was the cheapest and fastest. F cost 27% less than C at the same wall time. F's verification time includes the re-run at the merge-base (PR #4
  issue 4 not done); each automatic re-run took 12–13 s.

The tasks were far smaller than the caps: work calls cost USD 0.03–0.22 of their USD 3. A run that is to observe
compaction needs much longer phases.

## Why F answered 1 of 10

Both F probes answered from the session-start brief alone (no tool calls). Of the nine misses:

| miss | cause | what it is |
|---|---|---|
| verified (both phases), fresh (both phases), stale (phase 2) | The brief listed only `boltons-p1-split-camel` as verified, in both phases. The re-runs of the other three pieces passed but were marked "tests weakened or check declarations changed; needs review", so they were never verified. The worker had rewritten one import line of the test file to import the new function (`get_path)` → `get_path,` + `run_length)`); benchbook counts any guarded test file change that is not a pure addition as weakened. In phase 2 the guard compares with `boundary-base`, so camel-to-kebab, whose own test change only adds lines, inherited phase 1's import-line edit. A piece in this state appears nowhere in the brief: not verified, and not under "Reported done, not re-run". | the code under measurement (a false positive of the weakening rule) |
| discarded (both phases), finding (both phases) | The dead ends and the out-of-scope findings were stated only in the task prompts. benchbook records a discarded approach from a failing check run and a finding from a worker's report; the workers were not asked to report the finding (the lead wrote it in its own final message), and no check run failed on the dead end. The phase-2 brief did show two discarded approaches, but they were the phase-1 action's failing test runs against the harness's injected change. | the agent's choices and what benchbook captures |

C's probes read `claude-progress.txt`, `feature-list.json` and `git log`, all written by C's own lead; in phase 2
its lead had written the dead end and the finding into them.

**Scorer (post hoc, a sensitivity, not a replacement).** `bench/boundary/probes.py` requires each answer to equal the
expected identifier (case-insensitive). A's two `discarded` answers and C's phase-2 one are sentences that contain the
exact expected signature, so they were graded wrong. Counting an answer that contains the expected identifier as
correct gives A 8/10, C 7/10 and F 1/10. The registered H1 numbers above stand.

## Arm F: when the lead saw the re-run verdicts

Required by the amendment of 2026-11-24 ("when arm F's lead sees a re-run verdict"). Ordering is by transcript record
order and the runner's monotonic timestamps; this machine's wall clock steps by about ±55 s.

| verdict line | delivered to | integration decision for the piece | before the decision? |
|---|---|---|---|
| `boltons-p1-split-camel f5af48f: re-run passed … tests not weakened, checked: re-run` | lead, on a later Bash result (phase-1 work call) | `git merge` of the piece, issued before the verdict was written | no |
| `boltons-p1-run-length 07ee866: re-run passed … tests weakened …; needs review, checked: none` | lead, on the Bash result after the merge (phase-1 work call) | `git merge` of the piece | no (about 2 s after the merge returned) |
| `boltons-p2-weighted-median e16c45f: … needs review, checked: none` | lead, on the result of the merge call itself (phase-2 work call) | `git merge --no-ff` in that call | no |
| `boltons-p2-camel-to-kebab 6feff72: … needs review, checked: none` | lead, same delivery | same call | no |

- All four verdicts reached the lead and none reached a subagent; none went undelivered. Each "re-running …" notice
  reached the lead twice per piece, because a report is queued once at SubagentStop and once at PostToolUse.
- None reached the lead before it integrated the piece. After the "needs review" lines the lead diffed `tests/` and
  wrote in its report that nothing was weakened, after merging.
- None reached the action calls that wrote `release-phase-N.json`: notes are kept per session, and each action call
  resumed the probe's new session, which saw only the brief.
- The lead's four Agent results all had `status` `completed` (not `async_launched`), as
  `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` should give.
- Beside H2: arm F's instructions do not run `piece start` and the runner sets no `BENCHBOOK_PIECE`, so the
  TaskCompleted gate did not hold F's to-dos (PR #4 issue 1, as the amendment says).
- The trial's `lead.stream.jsonl` files do not carry the PostToolUse hook text, only the SessionStart brief. The
  deliveries above come from the Claude Code session transcripts, which the runner does not copy into the trial
  directory; a next run should copy them.

## Conditions that differ from the registration

- **Claude Code 2.1.331, not 2.1.330.** The installed CLI was updated on 2026-11-24, before the pilot, and all three
  trials ran on 2.1.331 (recorded as `claude_version` in `metrics.json`).
- One task and three trials instead of two tasks and six (lean amendment, written before any pilot call). Each arm ran
  in its own `--out` from `tasks-boltons.json`, with what was left of the USD 30 as `--total-budget-usd`.

## Files

| file | what |
|---|---|
| `report.md` | the runner's report over the three trials |
| `metrics.json` | the three trials' `metrics.json` merged: `trials` concatenated, `total_budget_usd` per arm |
| `tasks/tasks.json`, `tasks/tasks-boltons.json` | the registered manifest and the one-row manifest that ran (sha256 in the pre-registration) |
| `tasks/oracle_boltons.py`, `tasks/oracle_toolz.py` | the oracles, hidden tests included (written for this benchmark) |
| `tasks/reference/reference.py` | the reference solutions |
| `tasks/control-check.txt` | the control check of the oracles on base, reference and injected checkouts |

The task repositories are pytoolz/toolz and mahmoud/boltons (both BSD-3-Clause) at the upstream commits named in the
pre-registration; their code is not copied here. Raw streams, transcripts, worktrees and ledgers stay outside the
repository.
