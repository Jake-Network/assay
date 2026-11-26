# Pre-registration: benchbook swarm vs independent autoresearch

> Template (§13.1). Fill in every [bracket], commit this file, and only then run
> `python -m bench.autoresearch.launch ... --execute`. Do not edit it after the first repetition starts; add an
> "Amendments" section with a date if something has to change, and report it.

## Question

With the same GPUs, the same wall-clock time and the same agent, does a benchbook swarm (arm B) do better
than independent autoresearch agents (arm A)?

## Hypotheses

- **H1**: in every repetition, arm B has zero duplicate runs (metric 3) and zero unrecorded training attempts
  within the collector's scope. The collector and ledger match receipts by attempt ID, worker, code and seed.
  A collected run started outside `benchbook ar` also counts against H1. Ledger rows without collector receipts
  indicate incomplete observation and prevent an H1 pass.
- **H2**: arm B's replicated best val_bpb (metric 1) is lower than or equal to arm A's plus epsilon (B's
  calibrated epsilon, else 0.0005), averaged over the repetitions.
- **H3**: some of arm A's keeps do not reproduce (metric 5 for A is above 0), and fewer of B's do.

## Design

- **Repetitions**: at least 3 per condition, each a separate `launch.py --repetition k` with its own output
  directory and a fresh benchbook project; the same upstream commit, model and settings. Report every repetition and
  the mean (`python -m bench.autoresearch.report out1 out2 out3` writes summary.md). [N repetitions]
- **GPU groups**: the two GPU groups ([0-3] and [4-7]) may differ in NUMA node and PCIe switch. Each repetition
  assigns the groups to the arms by a coin flip, `--assign-seed k` (seeds [1, 2, 3]); the draw is recorded in
  conditions.json and the report. Both arms run at the same time.
- **Restarts**: if `claude -p` ends more than a minute before the deadline with one valid final cost receipt and
  remaining budget, restart the same prompt and worktree at most 3 times (`--max-restarts 3`). Each replacement
  session receives the original cap minus prior sessions' recorded spend. Count restarts per arm and report arms
  whose agents needed more than [3] restarts in total as degraded.

## Conditions

- Hardware: [GPU model] x [2N], one provider, one region. N per arm: [N].
- Wall clock per arm: [hours] h. Allocated GPU budget cap: [GPU hours, USD] (`--gpu-hours-per-arm`; defaults to
  wall-clock hours × GPU count). LLM budget cap: USD [X] per agent across sessions (`--max-budget-usd`).
- Agent: Claude Code [version], model [dated identifier], same permissions
  (`--allowedTools Bash,Read,Edit,Write,Glob,Grep`).
- Upstream: karpathy/autoresearch at commit [sha] (or fork [url] at [sha]); data prepared once with `uv run prepare.py`.
- benchbook: commit [sha]. Database: [embedded on one machine | Postgres URL, host only].
- **Both arms** use the same collector on PATH (wrap_train.sh + shim_log.py) and receive the same instruction to
  launch training with `uv run train.py`. The collector logs starts and ends for training script invocations
  via instrumented `uv run` and PATH `python`/`python3`, including relative and absolute script paths. `benchbook ar`
  tags each run with its attempt ID and replication number. A start without an end is retained. An absolute
  interpreter outside the supplied PATH or module-based invocation can bypass the collector, so report H1 within
  these observed paths and check adherence to the prescribed command separately.
- Arm A: upstream `program.md` unchanged; one branch per GPU.
- Arm B: `program.benchbook.md` from `benchbook ar init`; referee settings at their defaults
  (replicates 2, replicate_max_queue 4, epsilon_floor 0.0005, sanity_min 0.5, sanity_max_gain 0.1).
- Optional arm C, autoresearch-at-home: [included / not included, and why].

## Metrics (as computed by bench/autoresearch/report.py and post_replicate.py)

The unmodified upstream `train.py` (the baseline every arm runs first) is left out of metrics 3 and 5 in both
arms; its runs are reported on their own line.

1. Replicated best val_bpb: each arm's final best code re-run 3 times on the same hardware after the experiment
   (post_replicate.py); the mean. Match each A keep in results.tsv to a collected attempt from its own worker,
   using the code and printed metric. A's best is the lowest matched val_bpb. With unmatched keeps the A comparison
   is unavailable. B's best is the verified best, else the best candidate. post_replicate and report use the same
   definition.
2. Best over time: A as printed by kept runs, B as verified (candidates shown separately).
3. Duplicate runs and their GPU minutes: successful runs whose (normalised code, replication number) already
   ran successfully in the same arm. For A the replication number is always 0. B also reports how many runs
   benchbook refused before they started (refusals the agent overrode with `--retry-failed` do not count).
4. GPU minutes on crashes, timeouts and killed runs (shim).
5. False best ratio, one procedure for both arms: of up to 10 random non-baseline keeps per arm (sampling seed
   [0]; A: "keep" rows of results.tsv, valued by what the shim saw that code print; B: first runs that printed
   KEEP), the share whose 3-run re-run mean is worse than the kept value by more than epsilon (B's calibrated
   epsilon, else 0.0005). B's referee verdict counts are reported next to it but are not metric 5.
6. Share of B's GPU time on replication, and separately on calibration.
7. Claude tokens and dollars per arm (from the stream-json result events). Report observed spend and sessions
   with missing receipts separately. Repetition means require a complete set of values.
8. Experiments (first runs) per GPU hour.

## Stopping rules

- Stop both arms at the wall-clock limit or at either budget cap, whichever comes first.
- Stop both arms if a finished session has no auditable cost receipt. A CLI final result or budget-stop event can
  report charges incurred by the last request. The harness stops on that event and records observed usage.
- Stop early only if [a GPU fails / the provider interrupts]; report which arm, which repetition and when.

## Analysis

- Report every metric for both arms and every repetition, including the ones B loses. No metric is chosen
  after seeing results.
- H1 holds only if it holds in every repetition. H2 compares the means over repetitions with the epsilon margin
  above; with 3 repetitions this is a descriptive comparison, not a significance test, and is reported as such.
- Release rule (§3): ship if H1 holds across repetitions with collection coverage verified, even if H2 fails,
  as long as both arms' false-best ratios are published with it. Investigate failures or inconclusive collection
  before using H1 as a release gate.
