# benchbook: your agents' lab notebook

**Nothing they find gets lost, repeated, or trusted without evidence.**

<!-- 30-second GIF: the viewer while the circle-packing swarm runs (best-so-far curve rising,
     "duplicates blocked" counter going up). Record with: python examples/circle-packing/scripted_swarm.py -->

When several agents run experiments for you, their results come back as text to one lead agent. Some reports
get compacted away, some never arrive, and two workers happily run the same configuration. benchbook gives every
agent a shared Postgres ledger instead: workers record each experiment the moment it finishes (failures too),
check what was already tried before running anything, and hand the lead a 3-sentence summary with record ids.
The lead reads a budgeted brief built by SQL (best result, verified claims, what not to repeat) instead of
transcripts. In the circle-packing benchmark this did not yet lower the lead's peak context (see Benchmark).

## 60-second start

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook     # `uv tool install benchbook` once v0.1.0 is on PyPI; no Docker needed
uv tool update-shell        # once, if `benchbook` is not found; then open a new terminal and restart Claude Code
cd your-repo && benchbook init --goal "Minimise validation loss" --metric val_loss --direction min
claude plugin marketplace add Jake-Network/benchbook && claude plugin install benchbook@benchbook benchbook-mcp@benchbook
```

Then in Claude Code:

```
/benchbook-mcp:benchbook-lead Minimise val_loss of train.py; 3 rounds, 4 workers
```

and `benchbook serve` to watch it at http://127.0.0.1:7466.

No Claude Code? Everything is in the CLI and the Python SDK:

```bash
export BENCHBOOK_AGENT=worker-1                                                     # who is writing
benchbook exp tried  --config '{"lr": 3e-4, "warmup": 500}'                         # before running
benchbook exp record --config '{"lr": 3e-4, "warmup": 500}' --metrics '{"val_loss": 2.91}' --seed 0
benchbook brief
```

## autoresearch on many GPUs

**Run autoresearch on many GPUs as one lab: no repeated runs, nothing lost, and the swarm only builds on
results that replicated.**

<!-- 30-second GIF: the viewer's verified-best staircase while candidates pass or fail replication.
     Record with: python examples/autoresearch-sim/run_sim_swarm.py --agents 4 --minutes 5 --keep, then benchbook serve -->

```bash
cd autoresearch && uv run prepare.py                         # karpathy/autoresearch or any fork
bash /path/to/benchbook/integrations/autoresearch/launch_local.sh 4   # benchbook ar init + one worktree and agent per GPU
benchbook serve                                                  # watch it at http://127.0.0.1:7466
```

Each agent follows `program.benchbook.md` (upstream `program.md` with a shared loop). The same code never runs
twice on the same hardware; every run is recorded, crashes included; a result only becomes the shared
baseline after two other agents re-run it and the mean still wins. Self-hosted, no API key: several machines
need one Postgres URL. Details: [docs/autoresearch.md](docs/autoresearch.md).

| launch experiment (same GPUs, time and agent) | A: independent | B: benchbook |
|---|---|---|
| replicated best val_bpb | _tbd_ | _tbd_ |
| duplicate runs | _tbd_ | _tbd_ |
| keeps that did not reproduce / refuted | _tbd_ | _tbd_ |
| GPU time spent on replication | 0% | _tbd_ |

<!-- Filled from bench/autoresearch report.md after the pre-registered run (MANUAL_QA.md). -->

## Benchmark

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
[cost-breakdown.md](bench/results/2026-11-21-claude-sonnet-5-5-rerun/cost-breakdown.md): mostly more model calls,
each re-reading its context, rather than large tool results.

`python -m bench.run --dry` reproduces the pipeline without model calls.

## How it works

```
 worker-1 ─┐  check_tried → evaluate → record_experiment (+ claims with evidence)
 worker-2 ─┼──────────────────────────────►  Postgres ledger  (append-only)
 worker-3 ─┘                                       │  SQL: dedup, replicates, best, negatives
                                                   ▼
 lead  ◄── brief (2k-token budget: best, verified claims, "already tried", open questions) + ids
   └──► decide: supported / refuted / verified (non-LLM oracle + evidence only)
```

- **Nothing lost.** Every evaluation is a row the moment it ends. A hook captures any subagent output that
  cites no record, so even an agent that ignores the rules leaves a note.
- **Nothing repeated.** Same config + code + seed is refused; a different seed counts as a replicate. Briefs
  list what was already tried around your task, failures included.
- **Nothing trusted without evidence.** Workers propose claims. The lead or a human verifies them with a
  non-LLM oracle and evidence ids. The default rerun rule requires three seeds for the same configuration
  and code, including a rerun by a different agent, and the reruns must still beat the baseline: a result that
  was only re-run, not reproduced, is refused.

What these rest on, and what they do not: database triggers refuse UPDATE, DELETE and TRUNCATE on records
and keep each agent's role fixed, so an honest mistake (an agent "cleaning up test data") cannot rewrite
history. They do not stop someone who holds the database owner's credentials, which every agent on a shared
URL has today, and agent names are self-declared: an agent that lies about its name can pose as the lead or as
"another agent". Treat the guarantees as protection against mistakes, not against a hostile agent, on a shared
owner URL. Giving each machine the `benchbook_writer` login and a per-agent token binds every write to its agent
([Several machines](docs/multi-machine.md#per-agent-tokens)); the owner's own connection can still bypass it. See
[Honest limits](docs/concepts.md#honest-limits).

## What it fits

benchbook pays off when a job meets all three conditions:

- **A number per attempt.** Every attempt yields a metric (`val_bpb`, `sum_radii`).
- **Re-runnable.** The same config and code can run again with only the seed changed, so a rerun can verify a result.
- **One space, explored together.** Several workers search the same space at the same time, so reading each
  other's results removes repeats.

## What it is not

- Not a duplicate blocker for code implementation handed out to subagents. There is no number per attempt and
  approaches are free text, so duplicate blocking (an exact hash of config and `code_ref`) and seed-based rerun
  verification do not work there. What the `code` pack does for delegated coding is narrower: it re-runs the
  declared checks on a reported commit and records whether the completion held (next section).
- Not a memory layer for one agent or user (mem0, Letta).
- Not an orchestrator: it does not run, schedule or route agents. It sits under Claude Code, Codex or your own
  loop.
- Not an observability tool: traces stay in Langfuse/MLflow/Phoenix; experiments can carry a `trace_ref`.

## Coding work: done versus verified

An implementer saying "done" and a completion that an independent re-run confirmed are different records. With the
`code` pack (`benchbook init --pack code`) you declare the completion checks once, and the lead adds one line to
its integration step:

```toml
# .benchbook/config.toml
[checks.pytest-media]
cmd = "uv run pytest tests/media --junitxml={junit}"
junit = true
[guard]
paths = ["tests/**", "**/conftest.py", ".benchbook/config.toml"]
```

```bash
benchbook check verify media-upload-dedupe a1b2c3d --reported-by implementer-1
```

It re-runs the declared checks on that commit in a clean worktree, records the report and the re-run as a pair, and
notes whether the commit changed the tests or the check definitions (then a person decides, however green the
re-run). Also in the pack: `check run` and hooks that record the implementer's own runs, a completion gate for
Claude Code, path occupancy for pieces (`benchbook piece start`), capture of out-of-scope findings, and a GitHub
Action that posts one evidence summary per PR. Details: [docs/code.md](docs/code.md).

How often "done" is not done has not been measured yet; the harness and its pre-registration are in
`bench/code/`, and the status is in
[bench/results/2026-11-21-phase1/report.md](bench/results/2026-11-21-phase1/report.md).

## Docs

[Concepts](docs/concepts.md) · [Schema and SQL examples](docs/schema.md) · [Claude Code](docs/claude-code.md) ·
[Several machines](docs/multi-machine.md) · [Python SDK](docs/sdk.md) · [Circle-packing demo](examples/circle-packing/README.md) ·
[autoresearch on many GPUs](docs/autoresearch.md) · [Coding work](docs/code.md) · [Releasing](docs/releasing.md)

## Roadmap

v0.2: semantic near-duplicate warnings (pgvector), live MCP resource subscriptions, verified Codex setup,
OpenEvolve/ShinkaEvolve adapters, row-level security for teams. Later: domain packs (ML
research, Lean mathematics, lab science, finance close).

License: Apache-2.0.
