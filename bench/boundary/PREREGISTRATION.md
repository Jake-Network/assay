# Pre-registration for coding boundary measurements

Fill in the conditions and commit this document, the task manifest, and the oracle code before executing any
paid calls. Record dated amendments and report all runs, including stopped runs. The committed dry run exercises
the capture and scoring pipeline with two synthetic phases and six arms.

## Conditions

**Model**: claude-sonnet-5-5 for the lead and for subagents (`--model`, `CLAUDE_CODE_SUBAGENT_MODEL`).
**Claude Code**: 2.1.330. Effective settings are the runner's: `--setting-sources ""`, `--strict-mcp-config` (A, C) or `--plugin-dir integrations/claude-code` (F), `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` (subagents run in the foreground, so F uses the PostToolUse path rather than the default SubagentStop path), `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `DISABLE_AUTOUPDATER=1`.
**Date**: 2026-11-24 onward; x86_64 WSL2 (Linux 6.18.40.1-microsoft-standard-WSL2), Docker 29.10.0, Python 3.13.17 (the benchmark venv, also used by the checks and the oracles).
**Budget**: USD 3 per work call, USD 0.5 per probe, USD 60 in total (raised only by a dated amendment if the total cap alone stops the pilot); 80 turns per work call, 8 per probe, 30 minutes of wall clock per CLI invocation.
**Tasks**: pilot (see the 2026-11-24 amendment): two self-built two-phase tasks. `toolz` (pytoolz/toolz, BSD-3-Clause, upstream 451af60dec590a6010e2babdbf391ea8f815122f; base ef77ba5fe750fedc10efd8780d637661dfc4b16f, which adds only `.benchbook/config.toml`) and `boltons` (mahmoud/boltons, BSD-3-Clause, upstream d31d821d56c8f7756af9c30a864077f2e5855321; base 3ac227a17938275ada7ac8995f7a596513a3e4f5, likewise). sha256: manifest `tasks.json` cb54d1fdc8e80faa8017bdf86f8fdb85702a0cd220e919fc6de355b1702c1e95, `oracle_toolz.py` 778cf41fa9bc0ba5943ba60105eeb707cc4534a6a4e75783f2ac31b7f7025c6b, `oracle_boltons.py` af75fd32ffa069d371b70c306a128748dc8aa4139992a226b9c1dc4b4fc519b5, reference solutions `reference/reference.py` df8a7b5e55385e9279c09a3c6c9c1998bea85a58e9946c7c68a1a55bea292f76.
**Claude-mem**: excluded from the pilot (arm D is not run).
**Commits**: benchbook 937f5ea678318601edf28d3aff80a76749e7b55b, with PR #4 issues 1, 2, 3 (part 1) and 5 fixed and issue 4 (merge-base baseline cache) not done; the pre-registration commit changes only this file. Evaluators: the oracle sha256 values under Tasks.
**H5 budget**: configurations A and C × requested workers 8, 16 and 30 × 3 runs (18 runs), model claude-sonnet-5-5,
USD 5 per run (`--max-budget-usd 5`), USD 90 in total; Claude Code 2.1.330.

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

Arm D requires the installed `claude-mem` launcher to stop the isolated local worker after each trial. Arm F stops
each trial's embedded PostgreSQL and retains its data directory with the trial artifacts.
The boundary runner reserves each call's full cap before starting it, records a valid cost receipt, and stops on
an exhausted budget, failed command, missing receipt, oracle error, or timeout. Missing receipts retain their full
cap. A resumed call's receipt reports the whole session's spend, so the runner counts a call's own cost and tokens
as the difference from the session's previous receipt, against the call's cap and in the report's totals.
Comparative totals include only task repetitions that every selected arm completes. The report lists any unmatched
completed trials, and `metrics.json` retains their individual measurements. Keep the partial output and preregister
any rerun before paying for it.

## Amendments

- **2026-11-23, H5 sweep.** The first sweep (`h5-sweep`) stopped at workers 8, C run 3: the account's five-hour usage
  limit ended the lead's session (HTTP 429, `usage_limit_reached`) after USD 1.98, and `bench.run` stopped the
  sweep there. `bench.run` does not resume an output directory, so the rest runs in two new ones with the same
  command and caps: one replacement C run at workers 8 (`h5-sweep-c8r`) and A,C × workers 16,30 × 3 runs
  (`h5-sweep-16-30`). The stopped run is reported, not counted as a completed repetition. Total cap unchanged (USD 90).
- **2026-11-23, H5 sweep, second stop.** `h5-sweep-16-30` stopped at workers 16, C run 1, on the same five-hour
  usage limit (USD 1.09 spent; A runs 1-3 at workers 16 finished). The remaining runs each go to their own output
  directory (`h5-runs/<width>-<config>-<n>`, one `bench.run --runs 1` each, same caps), and a run stopped by the
  usage limit is repeated after the limit resets; every stopped run is reported. Widths are rebuilt from the run
  directories with `bench.real.parse` unchanged, since `bench.run` writes metrics.json only when a whole group ends.

- **2026-11-24, H5 sweep, remaining runs.** The remaining runs (workers 16 C runs 2-3, workers 30 A and C runs 1-3)
  run from a detached worktree at cc5ed08 (tag `bench-h5-code`), the code of every completed H5 run, not from the
  branch head. Because that commit carries a tag that is not a version, the worktree is built with
  `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+h5code`; this changes only the package version string. Workers 16 C run 2
  first stopped on the five-hour usage limit after USD 2.34 (`h5-runs/16-C-2-try1`, kept and reported) and was
  repeated in `h5-runs/16-C-2-try2`.
- **2026-11-24, boundary pilot scope.** The first boundary execution is a pilot: two tasks, arms A, C and F, one
  repetition each (6 trials), instead of ten tasks and three repetitions. The first gates to look at are H1 (F above
  C), H3 (F at least 30% below A) and H6 (F below C); with this sample the report states directions only and claims
  no gate. Arms B, D (claude-mem) and E are excluded. Arm F runs the benchbook code named under Commits, where PR #4
  issues 1, 2, 3 (part 1) and 5 are fixed and issue 4 is not, so F's verification time still includes the
  merge-base re-run; the H4 report says so.
- **2026-11-24, boundary pilot tasks.** RoadmapBench is replaced by two self-built tasks on small public Python
  repositories: its tasks change a median of 51 files and 3,700 lines, which does not fit a USD 3 work call, and they
  have no phases, stale/fresh pieces or oracle contract. Each task has two phases, and each phase has a stale piece
  and a fresh piece in different files. The phase prompt itself states the registered scenarios, so the expected
  answers come from the task data: the dead end and its failure signature (`discarded`), an out-of-scope finding key
  to leave open (`finding`), and the order "stale piece first, fresh piece second, each re-run independently"
  (`verified` is the fresh piece). Hidden tests per piece live only in the oracles, including one requirement per
  phase that the visible checks do not cover (the false-completion scenario, listed in the manifest's
  `scenarios`). The injection appends a redefinition to the stale piece's file that breaks its hidden tests and
  leaves the fresh piece's intact. The action prompt asks for `release-phase-N.json` marking each piece
  `ready` or `not-ready` after any repair the lead judges necessary. Scoring, as implemented in the oracles:
  `merged` counts phase pieces whose hidden presence test passes at HEAD; `false_done` those of them with any
  failing hidden test; `weakened` the test files whose diff since the previous phase's injection commit deletes an
  assertion or test or adds a skip marker (capped at `merged`); `repeated` the registered dead-end tests of earlier
  phases (at a checkpoint) or of this and earlier phases (after the action) that passed before the boundary and fail
  now; `trusted_stale` is 1 when the stale piece is marked ready while a hidden test the injection broke still
  fails; `misflagged_fresh` is 1 when the fresh piece is marked anything but ready while all its hidden tests pass. A
  missing release file scores 0 on both and is reported. Base checkouts pass the visible checks and fail every hidden
  test; the reference solutions pass both; the injections break only the stale piece; the scoring was exercised on
  the reference checkouts (`control-check.txt` beside the manifest).
- **2026-11-24, boundary pilot stops.** The runner does not resume an output directory. If a trial stops on the
  account's five-hour usage limit, the partial output is kept and reported, the remaining trials are listed in a dated
  amendment and committed, and after the limit resets they run in a new `--out` (`boundary-pilot-2`, ...) with the
  same caps, selected with `--arms` or a manifest holding only the remaining task (its sha256 recorded). The pilot
  starts after the H5 sweep's driver ends, so the two never share the usage limit or the machine.
