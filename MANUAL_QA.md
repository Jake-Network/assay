# Manual QA

Steps that need Claude Code or paid model calls. The automated parts of §18.3 run in CI:
scenario 1 is `scripts/qa_first_experiment.sh`, scenario 3 is `tests/test_scenarios.py`, and the hook is unit
tested with stored inputs (`tests/fixtures/hooks/`).

Setup for all scenarios:

```bash
uv tool install --from . benchbook        # or: pipx install .   (puts `benchbook` on PATH for the MCP server and hook)
cd /path/to/a/scratch/repo && git init
claude plugin marketplace add /path/to/benchbook
claude plugin install benchbook@benchbook benchbook-mcp@benchbook   # hooks; MCP server, worker and lead skill
```

## Scenario 2: `/benchbook-lead` runs circle packing for two rounds

1. Copy `examples/circle-packing/{evaluator.py,packing.py,seed_program.py}` into the scratch repo.
2. `benchbook init --goal "Maximize the sum of radii of 26 non-overlapping circles in the unit square." --metric sum_radii --direction max`
3. In another terminal: `benchbook serve`, open http://127.0.0.1:7466.
4. In Claude Code: `/benchbook-mcp:benchbook-lead Maximize sum_radii for 26 circles; evaluate with python evaluator.py <program.py>; 2 rounds, 4 workers`

Expected: tasks appear on the Tasks page and get claimed by `worker-1..4`; experiments and the best-so-far curve
update live; each worker's final message is <= 3 sentences plus ids; after round 1 the lead reads
`benchbook_status`/`benchbook_brief` rather than quoting worker messages; the final report lists best, verified claims,
experiment count, duplicates blocked and the viewer URL.

## Scenario 4: a subagent that does not write to the ledger is still captured

1. In the same repo, in Claude Code: "Use an benchbook-worker subagent to compute the sum of radii of the seed
   program (python evaluator.py seed_program.py) and report the number. Tell it not to use any benchbook tools."
2. Expected: `benchbook claim list --kind note` shows a claim starting with `[auto-captured]`, tag `auto-captured`,
   author `hook`; `benchbook get c:...` shows the full text in `payload.text` and the session id in `payload.source`.
3. `~/.local/state/benchbook/hook.log` has a `captured:c:...` line. A worker who cites an ID from a successful write
   in its own transcript produces `cited`.

## Benchmark (real)

```bash
uv run python -m bench.run --model sonnet --runs 3 --max-budget-usd 5
uv run python -m bench.run --model sonnet --runs 3 --max-budget-usd 5 --execute
```

The first command creates local command previews and does not start Claude or initialize a ledger. The second
uses the same Claude dollar cap, worker count, model, turn limit and wall-clock limit for each run. With the
shown settings, the nine requested runs have a nominal API budget of USD 45. Use a Claude Code version that
supports `--max-budget-usd`, `--forward-subagent-text` and `--json-schema`. Prefer a fixed model identifier
when comparing runs across dates. Each C run gets a separate benchbook project.

Look for `report.md` in the new output directory. It includes the conditions, measured API usage, and mean and
sample standard deviation across runs. The plot includes intermediate usage checkpoints when the streamed
message totals reconcile with the final Claude usage. Otherwise it includes only the measured endpoint for that
run. A failed run, timeout, or missing evaluator log, final report or usage stops the harness and leaves its raw
artifacts for inspection.

Each run saves its Claude Code session under a session ID chosen in advance and copies the lead's and the
subagents' transcripts into `<run>/transcripts/`; `command.json` names the originals. `metrics.json` has a
`usage_check` per run that reconciles per-message usage with the final totals (the stream carries each message's
output count from the start of its response, so only the transcripts reconcile all four token classes). C runs also
keep `hook-spool/` (the hook entries still queued when Claude exited) and `ledger.jsonl` (`benchbook export` of the
run's project). Then:

```bash
uv run python -m bench.analyze_cost bench/out/<run>   # writes bench/out/<run>/cost-breakdown.md
```

It splits each agent's prompt growth by what entered the context (benchbook tools, evaluation Bash, file reads and
writes, subagent reports), classifies repeated evaluations, and states for each cost hypothesis of the spec (§10)
the numbers that support or contradict it. `--compare <earlier>/metrics.json` adds the earlier batch's means.

Fill the README table from a complete comparison. The release gate in §1 requires C to have zero lost results,
a lower duplicate ratio and a lower peak lead context than A. Report whether C meets the best-per-token target.

## Platforms not verified by hand

- macOS (arm64, x86_64) embedded Postgres start-up: run `scripts/qa_first_experiment.sh` and `pytest`.

## Step 3: autoresearch (needs a GPU; the coding agent does not run these)

### Name and release (A0)

- [ ] Decide the distribution name, update the install commands in README.md, README.ko.md,
      docs/autoresearch.md and integrations/autoresearch/template/README.md, then tag `v0.1.0`.

### One-GPU smoke test (A5)

```bash
git clone https://github.com/karpathy/autoresearch && cd autoresearch && uv run prepare.py
uv tool install --from /path/to/benchbook benchbook
benchbook ar init --project smoke
export BENCHBOOK_AGENT=gpu0-smoke CUDA_VISIBLE_DEVICES=0
benchbook ar claim "baseline" && benchbook ar run -m "baseline"
```

Expected: `benchbook ar init` prints a partition like `h100-80gb/xxxxxxxx` (your GPU model); `run` takes about
5.5 minutes and prints `KEEP (baseline, calibration pending)  val_bpb=0.99…`; `results.tsv` has the header
and one `keep` row; `run.log` holds the full output; `benchbook ar status` shows 1 run and 2 open calibration tasks
(they need a second agent); the upstream `analysis.ipynb` reads `results.tsv`.

Then a second agent on the same machine (`BENCHBOOK_AGENT=gpu0-b`, a second worktree): `benchbook ar replicate --auto`
runs a calibration run in a temp directory without touching that worktree's `train.py` or `git status`. After
both calibration runs: `benchbook ar status` shows `calibration sigma=… epsilon=… (ok)` and a verified best.

Also check with an agent: `claude "Read program.benchbook.md and start."` in the worktree; it should loop through
replicate, sync, brief, claim, commit, run, keep/reset without asking questions.

### Launch experiment (§13)

Before running anything, decide and write into bench/autoresearch/PREREGISTRATION.md:

- [ ] GPU provider, GPU model, price per GPU hour (check at the time of the run).
- [ ] Total GPU budget cap (GPU hours and dollars) and the LLM budget cap per agent (`--max-budget-usd`).
- [ ] N GPUs per arm (recommended 4, minimum 2) and hours per arm (e.g. 8).
- [ ] The dated model identifier and the Claude Code version.
- [ ] Whether arm C (autoresearch-at-home) is included (only if it runs and its terms allow it).
- [ ] Commit PREREGISTRATION.md.

```bash
python -m bench.autoresearch.launch --upstream ~/autoresearch --gpus-a 0,1,2,3 --gpus-b 4,5,6,7 \
    --hours 8 --model <dated model id> --max-budget-usd <cap>           # preview: prints commands.sh
python -m bench.autoresearch.launch ... --execute                       # starts 2N agents, stops after --hours
python -m bench.autoresearch.post_replicate bench/out/autoresearch-... --gpu 0 --execute   # metrics 1 and 5
python -m bench.autoresearch.report bench/out/autoresearch-...
```

`post_replicate` without `--execute` prints how many training runs it needs (3 per arm plus 3 per sampled keep,
about 5.5 GPU minutes each). Fill the README table from report.md, including metrics B loses.
`python -m bench.autoresearch.launch --sim` checks the whole pipeline on the CPU simulator first.
