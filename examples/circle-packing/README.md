# Circle packing swarm

Pack 26 non-overlapping circles into the unit square and maximise the sum of their radii (the AlphaEvolve
benchmark; the best known value is about 2.635). Evaluation takes well under a second on a CPU.

| file | what |
|---|---|
| `evaluator.py` | runs a program and re-checks its circles itself (count, containment, overlap); never trusts reported scores. Violations within 1e-9 shrink every radius by that amount, so the tolerance cannot add score |
| `seed_program.py` | a 6x5 grid starting point (2.1667) |
| `packing.py` | configurable heuristics: `init` (grid, hex, random, lattice_perturb), `optimizer` (slsqp, penalty), `iters`, `perturb` |
| `scripted_swarm.py` | N worker processes and a scripted lead, no LLM |

Needs `numpy` and `scipy` (`pip install 'assay[examples]'`).

## (a) Scripted mode, one minute

```bash
python examples/circle-packing/scripted_swarm.py --workers 8 --minutes 1
assay serve --project circle-packing-26      # other terminal; open http://127.0.0.1:7466
```

Some workers are deliberately careless and re-run tried configurations, so you can see duplicates being refused.
The lead asks for replications of the best configuration and marks a claim `verified` (oracle `rerun`) once
three seeds from two different workers agree.

## (b) A real swarm in Claude Code

```bash
cp examples/circle-packing/{evaluator.py,packing.py,seed_program.py} /path/to/scratch-repo/
cd /path/to/scratch-repo && git init
assay init --goal "Maximize the sum of radii of 26 non-overlapping circles in the unit square." \
  --metric sum_radii --direction max
```

In Claude Code (with the plugin installed):

```
/assay:assay-lead Maximize sum_radii; workers write program files and score them with python evaluator.py <file>; 2 rounds, 4 workers
```

## (c) What to look at in the viewer

- **Overview:** the best-so-far curve, "duplicates blocked", verified claims, active agents.
- **Experiments:** filter by author or outcome; invalid runs show the evaluator's reason in notes.
- **Claims:** the verified card shows oracle `rerun` and its supporting experiments.
- **Tasks:** leases and who holds them; kill a worker and watch its lease expire and move.
