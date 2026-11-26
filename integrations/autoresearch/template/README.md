# autoresearch-swarm

Run [karpathy/autoresearch](https://github.com/karpathy/autoresearch) (or any fork) on many GPUs as one lab:
no repeated runs, nothing lost, and the swarm only builds on results that replicated. The shared research
ledger is [benchbook](https://github.com/Jake-Network/benchbook); this repository holds only the agent instructions.
It contains no autoresearch code: clone the original and attach benchbook to it.

## Start (one machine, several GPUs)

```bash
git clone https://github.com/karpathy/autoresearch && cd autoresearch && uv run prepare.py
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook
bash <(curl -fsSL https://raw.githubusercontent.com/Jake-Network/benchbook/main/integrations/autoresearch/launch_local.sh) 4
```

The last line runs `benchbook ar init`, makes one git worktree per GPU and prints one `claude` command per GPU.
Each agent reads `program.benchbook.md` (the upstream `program.md` with a shared loop).

## Several machines

Point every machine at one Postgres and use the same project id:

```bash
export BENCHBOOK_DATABASE_URL=postgresql://user:pass@db-host:5432/benchbook BENCHBOOK_PROJECT=autoresearch
export BENCHBOOK_AGENT=gpu0-$(hostname -s) CUDA_VISIBLE_DEVICES=0
benchbook ar init && claude "Read program.benchbook.md and start."
```

`benchbook ar` runs on the owner's URL: it does not support the writer login and per-agent tokens yet. `ar init`
creates the project, which the writer login may not, every agent is named by `BENCHBOOK_AGENT` rather than by its
token, and the referee writes under its own name, which a token connection refuses. Keep the database on a network
only the GPU machines reach. Replication still keeps a single fabricated value from becoming the shared baseline.

## Files

- `program.benchbook.md`: agent instructions, derived from upstream `program.md` (MIT). `benchbook ar init` writes
  the same file into your checkout.

Details: https://github.com/Jake-Network/benchbook/blob/main/docs/autoresearch.md
