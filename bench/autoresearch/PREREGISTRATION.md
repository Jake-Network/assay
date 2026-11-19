# Pre-registration: assay swarm vs independent autoresearch

> Template (§13.1). Fill in every [bracket], commit this file, and only then run
> `python -m bench.autoresearch.launch ... --execute`. Do not edit it after the run starts; add an
> "Amendments" section with a date if something has to change, and report it.

## Question

With the same GPUs, the same wall-clock time and the same agent, does an assay swarm (arm B) do better
than independent autoresearch agents (arm A)?

## Hypotheses

- **H1**: arm B has zero duplicate runs (same normalised train.py in the same partition and replication number)
  and zero runs that started but were not recorded.
- **H2**: arm B's replicated best val_bpb (metric 1) is lower than or equal to arm A's.
- **H3**: some of arm A's "keep" commits do not reproduce (metric 5 for A is above 0).

## Conditions

- Hardware: [GPU model] x [2N], one provider, one region. N per arm: [N]. A and B run at the same time.
- Wall clock per arm: [hours] h. GPU budget cap: [GPU hours, USD]. LLM budget cap: USD [X] per agent (`--max-budget-usd`).
- Agent: Claude Code [version], model [dated identifier], same permissions
  (`--allowedTools Bash,Read,Edit,Write,Glob,Grep`).
- Upstream: karpathy/autoresearch at commit [sha] (or fork [url] at [sha]); data prepared once with `uv run prepare.py`.
- assay: commit [sha]. Database: [embedded on one machine | Postgres URL, host only].
- Arm A: upstream `program.md` unchanged; one branch per GPU; the `uv` shim (wrap_train.sh) only records runs.
- Arm B: `program.assay.md` from `assay ar init`; referee settings at their defaults
  (replicates 2, replicate_max_queue 4, epsilon_floor 0.0005, sanity_min 0.5, sanity_max_gain 0.1).
- Optional arm C, autoresearch-at-home: [included / not included, and why].

## Metrics (as computed by bench/autoresearch/report.py)

1. Replicated best val_bpb: each arm's final best code (A: lowest "keep" across branches; B: verified best)
   re-run 3 times on the same hardware after the experiment (post_replicate.py); the mean.
2. Best over time: A as reported, B as verified (candidates shown separately).
3. Duplicate runs and their GPU minutes: runs of the same normalised code in the same arm beyond the first.
4. GPU minutes on crashes and timeouts.
5. False best ratio. A: of up to 10 random non-baseline keeps (sampling seed [0]), the share whose 3-run
   re-run mean is worse than the reported val_bpb by more than epsilon (B's calibrated epsilon, else 0.0005).
   B: (refuted + inconclusive) / judged results.
6. Share of B's GPU time spent on replication.
7. Claude tokens and dollars per arm (from the stream-json result events).
8. Experiments per GPU hour.

## Stopping rules

- Stop both arms at the wall-clock limit or at either budget cap, whichever comes first.
- Stop early only if [a GPU fails / the provider interrupts]; report which arm and when.

## Analysis

- Report every metric for both arms, including the ones B loses. No metric is chosen after seeing results.
- Release rule (§3): ship if H1 holds even if H2 fails, as long as A's false-best ratio is published with it.
  If H1 fails, do not ship; find the cause first.
