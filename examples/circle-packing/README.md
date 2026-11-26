# Circle packing swarm

Pack 26 non-overlapping circles into the unit square and maximise the sum of their radii (the AlphaEvolve
benchmark; the best known value is about 2.635). Evaluation takes well under a second on a CPU.

| file | what |
|---|---|
| `evaluator.py` | runs a program and re-checks its circles itself (count, containment, overlap); never trusts reported scores. Violations within 1e-9 shrink every radius by that amount, so the tolerance cannot add score |
| `seed_program.py` | a 6x5 grid starting point (2.1667) |
| `packing.py` | configurable heuristics: `init` (grid, hex, random, lattice_perturb), `optimizer` (slsqp, penalty), `iters`, `perturb` |
| `run_config.py` | scores one `packing.py` configuration and prints `evaluator.py`'s JSON line; the command workers run through `benchbook exp run` |
| `scripted_swarm.py` | N worker processes and a scripted lead, no LLM (they use the Python SDK in process) |

Needs `numpy` and `scipy` (`pip install 'benchbook[examples]'`).

## (a) Scripted mode, one minute

```bash
python examples/circle-packing/scripted_swarm.py --workers 8 --minutes 1
benchbook serve --project circle-packing-26      # other terminal; open http://127.0.0.1:7466
```

Some workers are deliberately careless and re-run tried configurations, so you can see duplicates being refused.
The lead asks workers to rerun the best configuration and code. After at least three seeds and a rerun by
a different worker, it verifies the observed minimum for the tested seeds. The decision records those seeds.

## (b) A real swarm in Claude Code

```bash
cp examples/circle-packing/{evaluator.py,packing.py,run_config.py,seed_program.py} /path/to/scratch-repo/
cd /path/to/scratch-repo && git init
benchbook init --goal "Maximize the sum of radii of 26 non-overlapping circles in the unit square." \
  --metric sum_radii --direction max
```

In Claude Code (after `benchbook install --mcp`):

```
/benchbook-mcp:benchbook-lead Maximize sum_radii over packing.py configs (init, optimizer, iters, perturb); workers evaluate each one with: benchbook exp run --config '<json>' --seed <n> --code-ref packing-v1 -- python run_config.py ; 2 rounds, 4 workers
```

Each evaluation is that one command: it refuses a config, code_ref and seed that already ran, otherwise runs
`run_config.py` (which reads the config and seed from the environment) and records the result, failures included
([exp run](../../docs/sdk.md#check-run-and-record-in-one-command)). Its last line names the record:

```
{"sum_radii": 2.6359317681882644, "max_violation": 0.0}
benchbook: recorded e:1a2b3c4d improved sum_radii=2.6359317681882644  ★ new best (ok, 1.3s)
```

Change `--code-ref` when `packing.py` or `evaluator.py` changes. Workers that write their own program files
score them the same way, with the program's hash as the code_ref:
`benchbook exp run --config '{"program": "p7.py"}' --code-ref <git hash-object p7.py> -- python evaluator.py p7.py`.

## (c) What to look at in the viewer

- **Overview:** the best-so-far curve, "duplicates blocked", verified claims, active agents.
- **Experiments:** filter by author or outcome; invalid runs show the evaluator's reason in notes.
- **Claims:** the verified card shows oracle `rerun` and its supporting experiments.
- **Tasks:** leases and who holds them; kill a worker and watch its lease expire and move.
