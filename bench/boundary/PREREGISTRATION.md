# Pre-registration for coding boundary measurements

Fill in the conditions and commit this document, the task manifest, and the oracle code before executing any
paid calls. Record dated amendments and report all runs, including stopped runs. The committed dry run exercises
the capture and scoring pipeline with two synthetic phases and six arms.

## Conditions

**Model**: [full model ID for lead and subagents]
**Claude Code**: [pinned version and effective settings]
**Date**: [run date and machine image]
**Budget**: [USD per work call, per probe and total; turns and wall clock]
**Tasks**: [ten fixed RoadmapBench tasks and source revisions, plus task manifest and evaluator hashes]
**Claude-mem**: [pinned plugin revision, worker provider, model and spending cap]
**Commits**: [benchbook revision, task adapter and evaluator revisions]
**H5 budget**: [per-run and total caps for the circle-packing sweep]

## Arms and schedule

- A uses one Claude Code session with normal auto memory and no delegation. The harness resumes its session
  between phases. B uses subagents with text handoffs and starts a fresh session after each phase.
- C adds the progress file and feature list pattern to B. The first session establishes both artifacts. Later sessions
  read them and git history, run checks before flipping the feature's pass flag, and record progress for the next
  session. [Anthropic's long-running agent harness](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
- D adds the pinned claude-mem plugin to B, with per-trial local storage. Capture its worker provider's cost
  separately. [Claude-mem installation](https://docs.claude-mem.ai/installation)
- E adds one search command over saved raw events from its own prior sessions to B.
- F uses B's identical delegation instructions with the hook-only benchbook plugin and an isolated ledger.
- Use the same tasks, phase prompts, cap, model, turn limits, and compact window across arms. Run each arm
  three times per task in the recorded order with a fresh checkout. Use a lowered auto-compact window of 100,000
  tokens. A session contributes a compaction sample only when its work stream records the actual boundary
  event. [Claude Code environment variables](https://code.claude.com/docs/en/env-vars)
  [Claude Code Agent SDK event reference](https://code.claude.com/docs/en/agent-sdk/typescript#sdkcompactboundarymessage)
- After work, the oracle establishes ground truth. The next invocation probes the completed work session with
  `--resume` if a compaction occurred. If the lead already responded after the boundary during its work call,
  count the probe in the delayed compaction column. Then change and commit one previously checked file. Launch the fresh
  or resumed session with the same five questions and run the phase's independent action prompt. The task should
  induce an observable decision about whether each piece can still be relied on, including the unchanged control.

## Metrics and gates

| Hypothesis | Measure | Pilot gate |
|---|---|---|
| H1 Preservation | Correct answers over questions at observed compactions and at session boundaries, separately | F exceeds C |
| H2 False completion | Merged pieces failing the hidden oracle over all merged pieces and number of merges weakening tests | F below each baseline |
| H3 Repeated failure | Reattempts of the same failed approach signature after a boundary | F at least 30% below A |
| H4 Cost | Total dollars, model usage tokens, wall time, peak lead input-plus-cache context, and probe overhead | F at most 20% above C in cost |
| H5 Fan-in | Peak lead context and estimated tokens in lead-facing Agent/Task result payloads at 8, 16 and 30 requested workers per round | Circle-packing C lower than A at each width and grows more slowly |
| H6 Stale claims | Reliance on a stale check as current and incorrect stale verdicts for the unchanged control, each over action prompts | F below C and D for stale reliance and zero false alarms |

Compare only matched completed trials and publish all missing or stopped trials. Claim a gate only after collecting
the minimum qualified samples fixed above. Report H1 denominators, whether compaction was observed, and any lead
activity before the probe. H5 shows actual delivered report counts beside configured worker counts. Costs for
external providers and any time outside Claude invocations must be captured independently for total-cost and
end-to-end wall time claims.

## Task adapter contract

Pass a JSON array to `--tasks`. Each row has an `id`, a local Git `source`, pinned full `base` SHA, a local Python
`oracle`, and at least two `phases`. Relative source and oracle paths are resolved against the manifest's directory.
Each phase provides a work `prompt`, a tracked `stale_path`, two distinct piece ids (`stale_piece` and `fresh_piece`),
a text suffix `injection`, and an `action` prompt after the boundary. Choose the suffix so it changes a file in the
stale piece's checked scope without touching the fresh piece. Work phases must finish with their tracked changes
committed. Register the false-completion and repeated-attempt scenarios in the task data. For benchbook, the
base must also declare the code checks, a `boundary` project mapping, a `boundary-base` guard base, and capture
rules for the delegated general-purpose agents. Use task prompts and independent hidden tests that work with
every arm.

The oracle entry point runs outside the worktree with Python isolated mode. It receives `--checkout`, `--base`,
`--phase` (1-based), `--stage` (`checkpoint`, `injected`, or `action`), `--streams` (the trial artifact directory),
and `--before-injection` (the prior SHA, at injected and action stages). It exits successfully with one JSON
object:

- At checkpoint and injected stages, `expected` has string fields `verified`, `discarded`, `finding`, `stale`,
  and `fresh`. Supply exact identifiers and an approach failure signature from independent evidence. The oracle
  must establish both verification controls before the change and only the unchanged control after it.
- Every receipt has nonnegative integers `merged`, `false_done`, `weakened`, `repeated`, `trusted_stale`,
  and `misflagged_fresh`. Count only new events at each stage. Count merged pieces and hidden oracle results at the
  checkpoint, and score the treatment of the two controls after the action using observed behavior. Preserve both
  raw streams and oracle receipts so results can be checked independently.

Keep the paid outputs outside the repository and run with a dedicated benchmark environment. Publish only
approved public task inputs and logs. Verify that the registered Claude Code and claude-mem versions activate
their expected instrumentation before starting the pilot.

## Run and stopping rules

```bash
uv run python -m bench.boundary.run --dry
uv run python -m bench.boundary.run --tasks /path/to/tasks.json
uv run python -m bench.boundary.run --execute --tasks /path/to/tasks.json --out /path/to/new-run \
  --model MODEL --claude-mem-plugin /path/to/plugin --max-budget-usd WORK_USD \
  --probe-budget-usd PROBE_USD --total-budget-usd TOTAL_USD
uv run python -m bench.boundary.report /path/to/new-run
uv run python -m bench.run --workers 8,16,30
```

The last command previews the separate H5 sweep. The existing `bench.run --execute` flag enables its paid calls.
Run the three worker widths at the registered per-run cap, compare matching completed repetitions, and include
stop reasons and delivered report counts.

Arm D requires the installed `claude-mem` launcher to stop the isolated local worker after each trial.
The boundary runner reserves each call's full cap before starting it, records a valid cost receipt, and stops on
an exhausted budget, failed command, missing receipt, oracle error, or timeout. Missing receipts retain their full
cap. A resumed call's receipt reports the whole session's spend, so the runner counts a call's own cost and tokens
as the difference from the session's previous receipt, against the call's cap and in the report's totals.
Keep the partial output and preregister any rerun before paying for it.
