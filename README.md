# benchbook: "done" is a claim until the tests say so

**Your coding agents report "done". benchbook re-runs the checks on that commit, flags tests that were weakened to
get there, and carries what was verified across subagents, sessions and compaction.**

A lead agent that splits work into pieces gets text back: "DONE a1b2c3d, all tests pass." Some of those reports are
wrong. Some got green by deleting an assert or adding a skip. The ones that were right are forgotten at the next
compaction or the next session, and a fresh agent tries the approach that already failed. benchbook keeps a Postgres
ledger beside the repository. A report becomes a completion claim, and only an independent re-run of the declared
checks on that exact commit, in a clean worktree, turns it into `verified`. Every session starts from what the ledger
says, not from what someone remembered.

## 60-second start (Claude Code)

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook   # `uv tool install benchbook` once v0.1.0 is on PyPI; no Docker needed
benchbook install --no-cli       # the benchbook hook plugin (--mcp adds the MCP server and the worker agent)
cd your-repo && benchbook init   # proposes the repository's test and lint commands as checks; .benchbook/ stays out of git
```

Then work as you do now. The one convention: an implementer ends its report with `DONE <sha>` (or a `commit: <sha>`
line), and the piece is its branch name, a `piece: <name>` line or a `Benchbook-Piece:` commit trailer.

## What happens in a session

| when | what benchbook does |
|---|---|
| a session starts, resumes or is compacted | gives the brief for what this branch changed (at most 1,500 tokens): verified pieces and how they were checked, re-runs nobody decided and why, "reported done, not re-run", discarded approaches, open findings, occupied paths. It is also written to `.benchbook/STATE.md` |
| right before compaction | asks the summary to keep the record ids and statuses verbatim |
| a subagent reports `DONE <sha>` | re-runs the declared checks on that commit in a clean worktree, in the background, as `rerun@<host>` (an orchestrator, so a pass decides `verified`) |
| the next prompt, Bash call or agent result | delivers the outcome once: `benchbook: media-upload a1b2c3d: re-run passed (2 checks, 14.2s), tests not weakened, checked: re-run (c:7f3a91c2)` |
| an agent runs a declared check in Bash | records the run against the piece and the commit |
| an agent marks a task completed on a piece's branch (`piece start`) | keeps it open until every declared check has a passing run at HEAD, and says which command to run |
| recording breaks (a dead report, hook failures) | starts the brief with `benchbook: recording stopped: ...` and the fix |

Hooks never write to stderr and always exit 0. Outside a benchbook project they leave without touching the database.

## What "verified" means

- **Checks are declared once and pinned.** `.benchbook/config.toml` `[checks]` names the commands, and the ledger pins
  them, so an implementer cannot redefine "passing" for its own piece. A commit that changes a check definition goes
  to a person.
- **The re-run is independent.** A clean worktree of the reported commit, run by the lead or by the hook's processor.
  The implementer's own runs are recorded too, but never count as the re-run.
- **Weakened tests go to a person, however green.** Deleted or changed assertions, new `skip`/`xfail` markers, any
  `conftest.py` change, a check running fewer tests than at the merge-base. Added tests are fine.
- **The environment is not the commit's fault.** A failure counts as an environment failure (a missing `.env`, a
  service that is down) only when the implementer's own clean run passed and the merge-base fails the same way. A goal
  test that fails at the base by design still counts against the piece.
- **Failures are remembered.** A failed re-run leaves a discarded approach with its failure signature, and the next
  agent's brief says not to repeat it.

Details: [docs/code.md](docs/code.md).

## Commands for the lead

```bash
benchbook check verify media-upload a1b2c3d --reported-by implementer-1   # the re-run by hand (CI, other harnesses)
benchbook brief --here            # the brief for what this branch changed
benchbook status                  # pieces verified, reported only and re-run mismatched; open findings; hook health
benchbook get c:7f3a91c2          # one record in at most 400 tokens
benchbook piece start media-upload --paths 'src/media/**'   # occupy paths so two implementers do not collide
```

`benchbook statusline` prints a one-line count for a status line. The [GitHub Action](integrations/github-action/README.md)
re-runs each piece a pull request completes and keeps one comment saying which completions an independent re-run
confirmed.

## Other harnesses

| harness | install | brief at session start | notes after tools | check runs recorded |
|---|---|---|---|---|
| Claude Code | `benchbook install` | SessionStart, also after compaction | UserPromptSubmit, PostToolUse | Bash |
| Codex | `benchbook install --harness codex` | SessionStart, also after compaction | PostToolUse | Bash, when the output carries the exit code |
| Cursor | `benchbook install --harness cursor` | sessionStart | postToolUse | Shell |
| Gemini CLI | `benchbook install --harness gemini` | SessionStart | AfterTool | `run_shell_command` |
| OpenCode V2 | copy [index.ts](integrations/opencode/README.md) | first model request | next model request | `shell` |
| Pi | copy [benchbook.ts](integrations/pi/README.md) | before the first agent turn | tool results | `bash` |

The automatic re-run after a report is Claude Code only. Elsewhere, `benchbook check verify` does the same.
[docs/harnesses.md](docs/harnesses.md) quotes each harness's documentation for every field read.

## Measured so far

**How often "done" is not done (SWE-bench Verified, 2026-11-23):** 7 of 39 completions that Claude Code
(`claude-sonnet-5-5`) reported done failed a declared check when benchbook re-ran it: **17.9%, 95% CI 9.0% to 32.7%**
(the hidden tests alone: 5 of 39, 12.8%). Only 3 of the 39 implementers ran a test before reporting done. The interval
contains the 10% threshold of the plan; the point estimate is above it. Details, including two refutations caused by
the visible check itself: [bench/results/2026-11-23-swe-verified/](bench/results/2026-11-23-swe-verified/README.md).
`bench/code/` is the harness. `bench/boundary/` measures what a lead still knows after compaction and restarts, across
six arms ([PREREGISTRATION.md](bench/boundary/PREREGISTRATION.md)).

**What a lead still knows after a restart (boundary pilot, 2026-11-25):** one task and one trial per arm, so directions
only. Asked five state questions in a new session after each phase, benchbook's boundary mode (F) answered 1 of 10,
against 6 of 10 for a single resumed session (A) and for progress files (C). Most of F's misses come from benchbook: a
test file whose import line a worker edited counted as weakened, so three of the four pieces never became verified in
the brief. No compaction happened (largest lead prompt 24k of a 100k window), and no arm trusted the stale check.
Details: [bench/results/2026-11-25-boundary-pilot/](bench/results/2026-11-25-boundary-pilot/README.md).

## Experiment swarms

benchbook started as a lab notebook for agents that run experiments, and that mode is unchanged. Workers record each
experiment the moment it finishes (failures too). They check what was already tried before running anything, and
hand the lead a short summary with record ids. The lead reads a budgeted brief built by SQL (best result,
verified claims, what not to repeat) instead of transcripts.

```bash
cd your-repo && benchbook init --goal "Minimise validation loss" --metric val_loss --direction min
benchbook install --no-cli --mcp
```

Then in Claude Code `/benchbook-mcp:benchbook-lead Minimise val_loss of train.py; 3 rounds, 4 workers`, and
`benchbook serve` to watch it at http://127.0.0.1:7466. Without Claude Code, the CLI and the Python SDK do the same:

```bash
export BENCHBOOK_AGENT=worker-1                                                     # who is writing
benchbook exp tried  --config '{"lr": 3e-4, "warmup": 500}'                         # before running
benchbook exp record --config '{"lr": 3e-4, "warmup": 500}' --metrics '{"val_loss": 2.91}' --seed 0
benchbook brief
```

### autoresearch on many GPUs

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

**Fan-in (H5, 2026-11-25).** The same task at 8, 16 and 30 requested workers per round, A against C, USD 5 cap per
run (mean over completed runs: 3 per configuration at 8 and 16; at 30, 2 for A and 1 for C):

| workers | lead context (max), A | lead context (max), C | report tokens to the lead, A | report tokens to the lead, C |
|---|---|---|---|---|
| 8 | 47.2k | 55.2k | 15.4k | 6.9k |
| 16 | 83.5k | 85.9k | 31.1k | 13.8k |
| 30 | 150.8k | 130.7k (Haiku workers) | 59.1k | 22.3k (Haiku workers) |

Pre-registered H5 gate, C lower than A at every width and growing more slowly: **not met**. C's lead reads less than
half of A's report tokens, but its peak context is higher at 8 and 16. At 30 neither C run is a like-for-like sample:
to stay under USD 5, one lead ran its workers on Haiku and the other ran 28 of 90 workers. Width 30 ran on Claude Code
2.1.331. Details: [bench/results/2026-11-25-h5-sweep/](bench/results/2026-11-25-h5-sweep/README.md).

`python -m bench.run --dry` reproduces the pipeline without model calls.

## How the ledger works

```
 worker-1 ─┐  report, check run, record_experiment (+ claims with evidence)
 worker-2 ─┼──────────────────────────────►  Postgres ledger  (append-only)
 worker-3 ─┘                                       │  SQL: re-runs, dedup, replicates, failures
                                                   ▼
 lead  ◄── brief (budgeted: verified, only reported, discarded, open findings) + ids
   └──► decide: supported / refuted / verified (non-LLM oracle + evidence only)
```

- **Nothing lost.** Every check run and every evaluation is a row the moment it ends. A hook captures any subagent
  output that cites no record, so even an agent that ignores the rules leaves a note.
- **Nothing repeated.** A failed approach is listed in the brief with its failure signature. For experiments, the
  same config + code + seed is refused, and a different seed counts as a replicate.
- **Nothing trusted without evidence.** Agents propose claims. The lead, a hook or a person verifies them with a
  non-LLM oracle and evidence ids: a clean re-run for code, replicated seeds that still beat the baseline for
  experiments.

What these rest on, and what they do not: database triggers refuse UPDATE, DELETE and TRUNCATE on records
and keep each agent's role fixed, so an honest mistake (an agent "cleaning up test data") cannot rewrite
history. They do not stop someone who holds the database owner's credentials, which every agent on a shared
URL has today, and agent names are self-declared: an agent that lies about its name can pose as the lead or as
"another agent". Treat the guarantees as protection against mistakes, not against a hostile agent, on a shared
owner URL. Giving each machine the `benchbook_writer` login and a per-agent token binds every write to its agent
([Several machines](docs/multi-machine.md#per-agent-tokens)); the owner's own connection can still bypass it. See
[Honest limits](docs/concepts.md#honest-limits).

## What it fits

benchbook pays off in two kinds of work.

- **Coding work with a test suite.** Pieces handed to agents, each ending at a commit that declared checks can
  judge. The more agents and sessions a change spans, the more a "done" needs a re-run behind it.
- **Experiments**, when all three hold:
  - **A number per attempt.** Every attempt yields a metric (`val_bpb`, `sum_radii`).
  - **Re-runnable.** The same config and code can run again with only the seed changed, so a rerun can verify a
    result.
  - **One space, explored together.** Several workers search the same space at the same time, so reading each
    other's results removes repeats.

## What it is not

- Not a code reviewer or a test writer. It re-runs the checks you declared. If they do not cover a requirement, a
  green re-run says nothing about it.
- Not a duplicate blocker for code implementation. Approaches are free text, so exact-hash dedup and seed replicates
  apply to experiments only. For code, what is remembered is which approaches failed and why.
- Not a memory layer for one agent or user (mem0, Letta).
- Not an orchestrator: it does not run, schedule or route agents. It sits under Claude Code, Codex, Cursor or your
  own loop.
- Not an observability tool: traces stay in Langfuse/MLflow/Phoenix; records can carry a `trace_ref`.

## Docs

[Coding work](docs/code.md) · [Claude Code](docs/claude-code.md) · [Other harnesses](docs/harnesses.md) ·
[Concepts](docs/concepts.md) · [Schema and SQL examples](docs/schema.md) · [Several machines](docs/multi-machine.md) ·
[Python SDK](docs/sdk.md) · [Circle-packing demo](examples/circle-packing/README.md) ·
[autoresearch on many GPUs](docs/autoresearch.md) · [Releasing](docs/releasing.md)

## Roadmap

v0.2: semantic near-duplicate warnings (pgvector), live MCP resource subscriptions, the automatic re-run for
harnesses other than Claude Code, OpenEvolve/ShinkaEvolve adapters, row-level security for teams. Later: domain packs
(ML research, Lean mathematics, lab science, finance close).

License: Apache-2.0.
