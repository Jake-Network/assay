# assay

**Your agents' lab notebook. Nothing they find gets lost, repeated, or trusted without evidence.**

<!-- 30-second GIF: the viewer while the circle-packing swarm runs (best-so-far curve rising,
     "duplicates blocked" counter going up). Record with: python examples/circle-packing/scripted_swarm.py -->

When several agents run experiments for you, their results come back as text to one lead agent. Some reports
get compacted away, some never arrive, and two workers happily run the same configuration. assay gives every
agent a shared Postgres ledger instead: workers record each experiment the moment it finishes (failures too),
check what was already tried before running anything, and hand the lead a 3-sentence summary with record ids.
The lead reads a budgeted brief built by SQL (best result, verified claims, what not to repeat) instead of
transcripts.

## 60-second start

```bash
uv tool install --from git+https://github.com/Jake-Network/assay assay     # PyPI name to be decided; no Docker needed
cd your-repo && assay init --goal "Minimise validation loss" --metric val_loss --direction min
claude plugin marketplace add Jake-Network/assay && claude plugin install assay@assay
```

Then in Claude Code:

```
/assay:assay-lead Minimise val_loss of train.py; 3 rounds, 4 workers
```

and `assay serve` to watch it at http://127.0.0.1:7466.

No Claude Code? Everything is in the CLI and the Python SDK:

```bash
assay exp tried  --config '{"lr": 3e-4, "warmup": 500}'                         # before running
assay exp record --config '{"lr": 3e-4, "warmup": 500}' --metrics '{"val_loss": 2.91}' --seed 0
assay brief
```

## Benchmark

<!-- Filled from bench/report.md after the real runs (MANUAL_QA.md). -->

Circle packing (n=26) with the same model, API budget, worker count, and wall-clock limit:

| | lost results | duplicate evaluations | lead context (max) | best per token |
|---|---|---|---|---|
| A: subagents report as text | _tbd_ | _tbd_ | _tbd_ | _tbd_ |
| B: subagents write JSON files | _tbd_ | _tbd_ | _tbd_ | _tbd_ |
| C: assay | _tbd_ | _tbd_ | _tbd_ | _tbd_ |

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
  and code, including a rerun by a different agent. Database triggers keep records append-only.

## What it is not

- Not a memory layer for one agent or user (mem0, Letta).
- Not an orchestrator: it does not run, schedule or route agents. It sits under Claude Code, Codex or your own
  loop.
- Not an observability tool: traces stay in Langfuse/MLflow/Phoenix; experiments can carry a `trace_ref`.

## Docs

[Concepts](docs/concepts.md) · [Schema and SQL examples](docs/schema.md) · [Claude Code](docs/claude-code.md) ·
[Several machines](docs/multi-machine.md) · [Python SDK](docs/sdk.md) · [Circle-packing demo](examples/circle-packing/README.md)

## Roadmap

v0.2: semantic near-duplicate warnings (pgvector), live MCP resource subscriptions, verified Codex setup,
OpenEvolve/ShinkaEvolve adapters, per-agent tokens and row-level security for teams. Later: domain packs (ML
research, Lean mathematics, lab science, finance close).

License: Apache-2.0.
