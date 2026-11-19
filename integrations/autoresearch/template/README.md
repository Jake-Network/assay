# autoresearch-swarm

Run [karpathy/autoresearch](https://github.com/karpathy/autoresearch) (or any fork) on many GPUs as one lab:
no repeated runs, nothing lost, and the swarm only builds on results that replicated. The shared research
ledger is [assay](https://github.com/Jake-Network/assay); this repository holds only the agent instructions.
It contains no autoresearch code: clone the original and attach assay to it.

## Start (one machine, several GPUs)

```bash
git clone https://github.com/karpathy/autoresearch && cd autoresearch && uv run prepare.py
uv tool install --from git+https://github.com/Jake-Network/assay assay
bash <(curl -fsSL https://raw.githubusercontent.com/Jake-Network/assay/main/integrations/autoresearch/launch_local.sh) 4
```

The last line runs `assay ar init`, makes one git worktree per GPU and prints one `claude` command per GPU.
Each agent reads `program.assay.md` (the upstream `program.md` with a shared loop).

## Several machines

Point every machine at one Postgres and use the same project id:

```bash
export ASSAY_DATABASE_URL=postgresql://user:pass@db-host:5432/assay ASSAY_PROJECT=autoresearch
export ASSAY_AGENT=gpu0-$(hostname -s) CUDA_VISIBLE_DEVICES=0
assay ar init && claude "Read program.assay.md and start."
```

Only share the database with people you trust: anyone who can connect can write. Replication still keeps a
single fabricated value from becoming the shared baseline.

## Files

- `program.assay.md`: agent instructions, derived from upstream `program.md` (MIT). `assay ar init` writes
  the same file into your checkout.

Details: https://github.com/Jake-Network/assay/blob/main/docs/autoresearch.md
