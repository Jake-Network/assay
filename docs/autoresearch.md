# autoresearch on many GPUs

[karpathy/autoresearch](https://github.com/karpathy/autoresearch) gives one agent one GPU: it edits
`train.py`, trains for a fixed 5 minutes, keeps the commit if `val_bpb` went down and resets otherwise.
With more GPUs the only option is one branch per GPU, and the branches share nothing.

`benchbook ar` attaches a shared ledger to any autoresearch checkout (upstream or a fork) so that the agents work
as one lab:

- **No repeated runs.** The same code (comments and formatting ignored) never runs twice on the same hardware
  class, and it is refused *before* the GPU starts. Ideas in progress are visible to everyone.
- **Nothing lost.** Every run that `benchbook ar run` starts is recorded, crashes, timeouts and interruptions
  included. If the database is unreachable the run waits in a local queue and is written by the next command;
  its intent (or replication task) stays open until then.
- **Only replicated results become the baseline.** A run that beats the verified best is a *candidate*. Two
  other agents re-run the same code automatically; only if the mean still wins does it become *verified*,
  and only verified code is what `benchbook ar sync` hands to everyone.

Self-hosted, no API key: one machine needs nothing else; several machines need one Postgres URL. benchbook does
not copy or modify autoresearch code.

## Scenarios

| | situation | what to do |
|---|---|---|
| S1 | one machine, N GPUs | `integrations/autoresearch/launch_local.sh N`: one git worktree and one agent per GPU, local embedded Postgres |
| S2 | several machines | the same `BENCHBOOK_DATABASE_URL` and `BENCHBOOK_PROJECT` everywhere (see [Several machines](multi-machine.md)) |
| S3 | you already ran autoresearch alone | `benchbook ar import results.tsv --hw <gpu class> --agent <name>`: each row's `train.py` comes from git; `--hw` names the GPU those runs used (required: results.tsv does not say); your old keeps become candidates and get replicated |
| S4 | macOS, MLX, Windows or AMD fork | `--hw` / `BENCHBOOK_AR_HW` when detection fails, `--cmd` for the run command, `--parse-regex` for the summary |

### S1 in detail

```bash
cd autoresearch && uv run prepare.py                     # once
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook
bash /path/to/benchbook/integrations/autoresearch/launch_local.sh 4   # prints 4 commands, one per GPU
benchbook serve                                              # viewer at http://127.0.0.1:7466
```

Each printed command is `cd ../ar-gpuN && CUDA_VISIBLE_DEVICES=N BENCHBOOK_AGENT=gpuN-<hostname> BENCHBOOK_PROJECT=... claude
'Read program.benchbook.md and start.'`. Run them in separate terminals (or `AR_TMUX=1` to open tmux windows).

### S2 in detail

On every machine:

```bash
export BENCHBOOK_DATABASE_URL=postgresql://benchbook:...@db-host:5432/benchbook   # TLS for a managed Postgres
export BENCHBOOK_PROJECT=autoresearch BENCHBOOK_AGENT=gpu0-$(hostname) CUDA_VISIBLE_DEVICES=0   # unique per GPU and machine
cd autoresearch && benchbook ar init && claude "Read program.benchbook.md and start."
```

Run the database either with this repository's `docker compose up -d` on one machine (restrict the port with a
firewall to your machines) or as a managed Postgres with TLS. `.benchbook/config.toml` may be committed; the URL
never goes into it.

## Comparison groups (partitions)

Results are only compared within a **partition**: `<hardware class>/<prepare.py hash>`, e.g.
`h100-80gb/1a2b3c4d`. The time budget is fixed, so different GPUs give incomparable `val_bpb`; and a fork that
changes `TIME_BUDGET`, `EVAL_TOKENS`, `MAX_SEQ_LEN` or the evaluation gets a different `prepare.py` hash.

- Hardware class: `nvidia-smi --query-gpu=name,memory.total` for the first device in `CUDA_VISIBLE_DEVICES`,
  lower-cased without vendor and memory-type words (`NVIDIA H100 80GB HBM3` → `h100-80gb`); on macOS the CPU
  brand (`apple-m3-max`); otherwise `unknown-<hostname>`. Override with `--hw` or `BENCHBOOK_AR_HW`. An installed
  `nvidia-smi` that fails (three tries) is an error, not `unknown-<hostname>`.
- Code identity: sha256 of a canonical dump of `ast.parse(train.py)` (node types and non-empty fields, no
  positions), so comments, blank lines and quote style do not matter, and the hash is the same on Python
  3.11 to 3.13. `ast.unparse` is not used: its output differs between versions (3.12 writes f-strings with
  other quotes), which split dedup between machines. The CLI checks both hash formats when looking for prior
  runs. If the legacy harness hash matches an existing partition, the checkout keeps that partition and
  its verified baseline, and the worker can claim its existing replication jobs. The ledger remembers the
  detected partition alias. If the old Python version rendered your harness differently, run
  `BENCHBOOK_AGENT=gpu-0 python -m benchbook ar status` once with the upgraded package installed in that original
  interpreter to discover the alias. A file that does not parse is hashed raw (`raw-…`).
- Knobs: every module-level `UPPER_CASE = value` in `train.py`, evaluated safely (`2**19` → 524288; anything
  else is kept as an expression). `H100_BF16_PEAK_FLOPS` is ignored in knob changes (`knob_ignore`).
- Parent: the committed `train.py` one commit back (or `HEAD` when you run uncommitted changes). The run is
  linked to the parent's experiment, which builds the lineage tree.

## Commands

All commands run in the checkout root (where `train.py` and `prepare.py` are), accept `--json`, and use the
exit codes 0 ok, 1 user error, 2 refused (duplicate or conflict), 3 nothing to do. `BENCHBOOK_AGENT` names the agent.

| command | what it does |
|---|---|
| `benchbook ar init [--project ID]` | creates the project (pack ml-research, metric val_bpb min), writes `program.benchbook.md`, `.benchbook/config.toml` and `.gitignore` entries, prints the partition and the start prompt |
| `benchbook ar brief` | ~1,500 tokens: partition and noise, verified best, candidates, other agents' work in progress, knob history, code changes, observations and ideas |
| `benchbook ar claim "<idea>" [--knob K=V ...] [--force]` | announces the idea (15 min lease). Refused if another live intent declares the same knob changes; a very similar description (word Jaccard ≥ 0.8) needs `--force`; earlier runs of the same change are shown |
| `benchbook ar run -m "<idea>" [--cmd "uv run train.py"] [--timeout 600]` | refuses code that already ran or is running in the partition; runs exactly the bytes it hashed in a temporary copy of the checkout with output in `run.log`, kills the process group on timeout or interruption, records the result and code, appends to `results.tsv`, and asks the referee. Prints `KEEP (…)` on local improvement or the first successful run without a measured parent, `DISCARD (…)`, `INVALID (…)` (over the time budget), or `CRASH (…)`. The referee selects shared candidates separately. |
| `benchbook ar replicate --auto [--max 1]` | takes a replication or calibration task meant for you and runs that code in a temporary directory: your `train.py` and git are untouched. The command is the one the original run used when the task carries it, else `--cmd`. Exit 3 when there is none |
| `benchbook ar sync [--trust verified\|candidate]` | adopts the partition's verified best when it beats your code or your code builds on a refuted or inconclusive candidate; commits `train.py` |
| `benchbook ar note "<text>"` / `benchbook ar idea "<text>"` | an observation (evidence: your last run) / a hypothesis for later |
| `benchbook ar import [results.tsv] --hw <class>` | records an existing results.tsv (code from `git show <commit>:train.py`) as first runs in the partition of `--hw` and that commit's `prepare.py`; improvements become candidates |
| `benchbook ar status [--all]` | per partition: verified best, candidates, in flight, replication queue, verdicts, calibration, duplicates blocked (refused before running and not retried with `--retry-failed`), GPU minutes on replication |
| `benchbook ar referee [--interval 30] [--once]` | applies the referee rules on a timer. Optional: every `ar run/replicate/sync` already does |

The loop the agents follow is in `program.benchbook.md`: replicate → sync → brief → claim → edit and commit → run
→ keep or reset → note.

## The referee

A deterministic function (agent `referee`, role orchestrator, no LLM) that runs after every `ar run`,
`ar replicate` and `ar sync`, serialised by an advisory lock and idempotent.

| rule | when | what |
|---|---|---|
| T1 | a partition has its first successful run | two calibration tasks re-run that code (not by its author), priority 20 |
| T2 | the calibration runs are done | sigma = sample std of the 3 values; epsilon = max(2·sigma, epsilon_floor); stored in settings and as a calibration claim |
| T3 | a first run is `improved` (beats the verified best by epsilon) | `replicates` tasks (default 2) for other agents, priority 10 |
| T4 | val_bpb < `sanity_min` or a gain over the verified best > `sanity_max_gain` | a "suspicious" observation; the run is replicated like any other |
| T5 | all replication tasks of a candidate ended | verified when every run succeeded and both the mean of all runs and the mean of the replications alone beat the verified best by epsilon; refuted otherwise; inconclusive when a run failed or produced nothing. Verified passes the ledger's rerun rule (3 distinct replication numbers, 2 authors) |
| T6 | a replication task failed for good | the candidate is inconclusive |
| T7 | a new verified best | queued replications whose first run is worse than the new best + epsilon are cancelled |

Until calibration finishes, `epsilon_floor` (0.0005) is used. The first code of a partition is verified by
its calibration runs and becomes the first verified best.

Note on replication numbers: upstream `train.py` fixes its seed to 42, so the ledger's `seed` field holds the
replication number (0 = first run, 1, 2 = replications), not a random seed. The run-to-run noise comes from the
time budget (step counts differ) and GPU kernels.

### Settings

`benchbook ar init` sets these in the project settings (change them with `Ledger.update_settings`, as orchestrator
or human; each change is audited in `settings_events`):

| setting | default | meaning |
|---|---|---|
| `replicates` | 2 | replications per candidate |
| `replicate_max_queue` | 4 | candidates waiting for replication per partition; a better arrival pushes out the weakest idle one |
| `epsilon_floor` | 0.0005 | smallest improvement that counts |
| `sanity_min`, `sanity_max_gain` | 0.5, 0.1 | suspicious-result thresholds |
| `knob_ignore` | `["H100_BF16_PEAK_FLOPS"]` | constants left out of knob changes |
| `outcome_baseline` | `verified` | `improved` means better than the verified best (the raw best before any verification); such a first run is a candidate for replication |
| `time_budget_s` | `TIME_BUDGET` in `prepare.py`, else 300 | a run whose `training_seconds` exceeds it by more than `time_budget_slack_s` (30), or whose wall clock exceeds it by more than `time_budget_overhead_s` (240, startup and evaluation), is recorded `invalid`; 0 turns the check off |

## Cost

Replication costs GPU time: by default 10 minutes (2 × 5) per candidate, plus 10 minutes of calibration per
partition. Control it with `replicates` and `replicate_max_queue`; T7 cancels replications that can no longer
win. `benchbook ar status` and the viewer always show the share of GPU minutes spent on replication, and the
launch experiment reports it.

## Known limits

- Validity is checked only against the time budget. `val_bpb` is still the last value `train.py` prints, so a
  `train.py` that prints a made-up value and reproduces it is verified; `training_seconds` is self-reported too
  (the wall-clock check is not). Calling the evaluation in `prepare.py` from outside `train.py` would need a
  model checkpoint contract that upstream does not have; it is not done.
- The noise estimate uses three calibration runs, so epsilon can be small by chance (see the referee).

## Trust model

Anyone who can connect to the database can write to it: share it only with collaborators you trust. benchbook
has no accounts or authentication. Replication limits the damage of a fabricated or lucky value: a single run
never becomes the shared baseline, and suspicious values are flagged.

## Viewer

`benchbook serve` (http://127.0.0.1:7466) shows, per partition: the verified-best staircase with candidates and
refuted results, counters (verified best, waiting for replication, in flight, duplicates blocked and GPU
minutes saved, replication share), work in progress, a lineage page (the path from the verified best to the
root, and the whole tree), a knob history table, and for each run the `train.py` diff against its parent.

## Forks

| fork type | what to pass |
|---|---|
| different run command | `--cmd "python train.py"` on `ar run` and `ar replicate` |
| different summary format | `--parse-regex '^(\w+)=([-\d.eE+]+)$'` (two groups: key, number) |
| no `nvidia-smi` (MLX, ROCm) | `BENCHBOOK_AR_HW=m3-max-128gb` (any stable name for comparable machines) |

## FAQ

**Is this autoresearch-at-home?** No. autoresearch-at-home (as of its last commit, 2026-03-12) coordinates through a hosted key-value service
(Ensue, `ENSUE_API_KEY`), blocks claims whose description embedding is ≥ 0.92 similar, keeps a best per VRAM
tier and accepts a new global best after sanity checks (val_bpb ≥ 0.5, at most 0.1 better at once, re-read
before write). benchbook is self-hosted, compares exact normalised code and declared knob changes instead of
embeddings, groups by GPU model and evaluation harness, and only promotes a result after other agents
replicate it.

**Does an agent wait for verification?** A KEEP lets the agent build on its local result immediately. `sync`
moves it to the verified best later if the candidate is refuted or a better result is verified.

**Is KEEP the same as upstream's keep?** Yes: KEEP means the run beat its parent (the code you last kept,
mean of its runs) by epsilon, as upstream compares with the previous keep. Separately, a first run that beats
the partition's verified best is a candidate and gets replicated. A run can be a candidate and still print
DISCARD (it beats the verified best but not your parent); the referee may replicate it, and `sync` adopts it
if it is verified.

**What happens when `benchbook ar run` is killed?** Ctrl-C, SIGTERM and SIGHUP kill the training's process group,
the run is recorded as an error ("interrupted") and the intent is released; retry with `--retry-failed`. On
Linux the training also gets SIGTERM when `benchbook ar run` is killed outright (SIGKILL), but then nothing is
recorded and the intent expires after its lease. Give the command a long tool timeout or run it in the
background (`program.benchbook.md` says so).

**What if an agent runs `uv run train.py` directly?** That run is invisible to the ledger. `program.benchbook.md`
forbids it; a PATH shim that redirects it to `benchbook ar run` is planned (`ar init --shim`).

**One agent alone?** It works, but nothing gets verified: calibration and replication need agents other than
the original runner.
