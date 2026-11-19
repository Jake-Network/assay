# Manual QA

Steps that need Claude Code or paid model calls. The automated parts of §18.3 run in CI:
scenario 1 is `scripts/qa_first_experiment.sh`, scenario 3 is `tests/test_scenarios.py`, and the hook is unit
tested with stored inputs (`tests/fixtures/hooks/`).

Setup for all scenarios:

```bash
uv tool install --from . assay        # or: pipx install .   (puts `assay` on PATH for the MCP server and hook)
cd /path/to/a/scratch/repo && git init
claude plugin marketplace add /path/to/assay
claude plugin install assay@assay
```

## Scenario 2: `/assay-lead` runs circle packing for two rounds

1. Copy `examples/circle-packing/{evaluator.py,packing.py,seed_program.py}` into the scratch repo.
2. `assay init --goal "Maximize the sum of radii of 26 non-overlapping circles in the unit square." --metric sum_radii --direction max`
3. In another terminal: `assay serve`, open http://127.0.0.1:7466.
4. In Claude Code: `/assay:assay-lead Maximize sum_radii for 26 circles; evaluate with python evaluator.py <program.py>; 2 rounds, 4 workers`

Expected: tasks appear on the Tasks page and get claimed by `worker-1..4`; experiments and the best-so-far curve
update live; each worker's final message is <= 3 sentences plus ids; after round 1 the lead reads
`assay_status`/`assay_brief` rather than quoting worker messages; the final report lists best, verified claims,
experiment count, duplicates blocked and the viewer URL.

## Scenario 4: a subagent that does not write to the ledger is still captured

1. In the same repo, in Claude Code: "Use a general-purpose subagent to compute the sum of radii of the seed
   program (python evaluator.py seed_program.py) and report the number. Tell it not to use any assay tools."
2. Expected: `assay claim list --kind note` shows a claim starting with `[auto-captured]`, tag `auto-captured`,
   author `hook`; `assay get c:...` shows the full text in `payload.text` and the session id in `payload.source`.
3. `~/.local/state/assay/hook.log` has a `captured:c:...` line. A worker that ends with record ids produces `cited`.

## Benchmark (real)

```bash
python -m bench.run --model sonnet --runs 3                 # prints the claude -p commands; check flags with claude --help
python -m bench.run --model sonnet --runs 3 --execute       # paid run; writes bench/out/<ts>/report.md
```

Expected: `report.md` with a row per configuration, `curve.png`, and the conditions block. Fill the README
benchmark table from it. Release gate (§1): C has 0 lost results and a lower duplicate ratio than A; if C does not
win on best-per-token, say so in the README.

## Platforms not verified by hand

- macOS (arm64, x86_64) embedded Postgres start-up: run `scripts/qa_first_experiment.sh` and `pytest`.
