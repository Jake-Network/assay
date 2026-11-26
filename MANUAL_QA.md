# Manual QA

Checks that need a real Claude Code session, another harness, a GPU or paid model calls. CI covers the rest:
`scripts/qa_first_experiment.sh` (scenario 1), `tests/test_scenarios.py` (scenario 3) and the hooks with stored inputs
(`tests/fixtures/hooks/`). Anything marked paid needs the owner's approval (CLAUDE.md, "Paid runs").

Setup: install as in [docs/claude-code.md](docs/claude-code.md#install) (`benchbook install --no-cli --mcp`), or
run a checkout's plugins with `claude --plugin-dir /abs/path/to/benchbook/integrations/claude-code --plugin-dir
/abs/path/to/benchbook/integrations/claude-code-mcp` (absolute paths: the session starts in the scratch repository).
`--plugin-dir` installs nothing, so `benchbook doctor`'s `claude code plugin` line fails there; check that line only
with an installed plugin.

## Code-mode session

A scratch repository with one non-JUnit check, a piece and a worktree for its branch:

```bash
mkdir -p /tmp/bb-qa && cd /tmp/bb-qa && git init -q -b main
printf 'def f():\n    return 1\n' > app.py
printf 'test:\n\tpython3 -B -c "import app; assert app.f() == 1"\n' > Makefile   # -B: no __pycache__, the tree stays clean
git add app.py Makefile && git commit -qm init
benchbook init --yes                                    # declares check unit = make test; .benchbook/ stays out of git
benchbook --agent lead piece start qa-worker --paths app.py
git worktree add -q ../bb-qa-worker -b qa-worker
```

Then:

- **Completion gate.** Start a second Claude Code session in `../bb-qa-worker` and, before any passing run exists, mark
  a task completed: it is held (exit 2) and the message names `benchbook check run unit --piece qa-worker`. Run that
  command there and mark the task completed again: it goes through.
- **Re-run and verdict.** In a session in `/tmp/bb-qa`, have an implementer subagent change `app.py` in
  `../bb-qa-worker`, commit, and end its report with `DONE <full sha>`. Let the lead do something else than merging
  until the verdict arrives: the "re-running" line and then the verdict each arrive once. A `git merge qa-worker`
  after that goes through and does not repeat the verdict.
- **Merge gate, verdict in time.** Repeat with a new commit, and have the lead run `git merge qa-worker` as its first
  tool call after the report: the merge waits for the re-run (at most 30 s), goes through with the verdict as context,
  and the verdict does not arrive a second time afterwards.
- **Merge gate, verdict late.** A third commit adds `import time; time.sleep(120)` at the top of `app.py` and is
  reported the same way; the lead runs `git merge qa-worker` within a minute of the report, so the 30 s wait ends
  before the re-run does. That merge is denied once with the wait as the reason; the next one goes through.
- **Bash check.** `make test` run in Bash on a clean tree shows in `observed.jsonl` (kind `direct`) and as an attempt with
  `via = bash-hook`.
- **Doctor.** `benchbook doctor` passes (with an installed plugin). With `benchbook` removed from PATH, a hook run
  makes `hook cli` fail.

## Scenario 2: `/benchbook-lead` runs circle packing for two rounds

1. In a new scratch repository (not the code-mode one, whose project `init` would reuse), copy
   `examples/circle-packing/{evaluator.py,packing.py,seed_program.py}` into it.
2. `benchbook init --goal "Maximize the sum of radii of 26 non-overlapping circles in the unit square." --metric sum_radii --direction max`
3. In another terminal: `benchbook serve`, open http://127.0.0.1:7466.
4. In Claude Code: `/benchbook-mcp:benchbook-lead Maximize sum_radii for 26 circles; evaluate with python evaluator.py <program.py>; 2 rounds, 4 workers`

Expected: tasks appear on the Tasks page and get claimed by `worker-1..4`; experiments and the best-so-far curve
update live; each worker's final message is a short summary plus ids; after round 1 the lead reads
`benchbook_status`/`benchbook_brief` rather than quoting worker messages; the final report lists best, verified
claims, experiment count, duplicates blocked and the viewer URL.

## Scenario 4: a subagent that does not write to the ledger is still captured

Ask for a benchbook-worker subagent that computes the seed program's sum of radii (`python evaluator.py
seed_program.py`) without any benchbook tools. Expected: `benchbook claim list --kind note` shows a claim starting
with `[auto-captured]`, tag `auto-captured`, author `hook`, with the full text in `payload.text` and the session id in
`payload.source`, and `hook.log` has a `captured:c:...` line. A worker that cites an ID from a successful write in its
own transcript produces `cited`.

## Scenario 5: what Claude Code does at the boundaries (paid)

Run on each new Claude Code version:

```bash
scripts/qa_boundary_hooks.sh --keep              # haiku by default; --model sonnet to match a real lead
scripts/qa_boundary_hooks.sh --save-fixtures     # also writes what it saw to tests/fixtures/hooks/
```

The script's header lists its scenarios 1-7 (startup, compaction, foreground and background subagents, Bash, both
plugins, a subagent's hook inputs, the merge gate). It installs recorder hooks through `--settings`, so your own
settings do not change, and prints a table (also `report.md` in the kept directory). Expected is the "documented"
column of [docs/claude-code.md](docs/claude-code.md#boundary-hooks-what-claude-code-does), with `completed` or
`async_launched` for the foreground Agent call. Copy what it saw, with the Claude Code version, into the "observed"
column; the fixture list is there too.

## Other harnesses

For Codex, Cursor and Gemini CLI: `benchbook install --no-cli --harness <name> --dry-run`, then without
`--dry-run`. In Cursor and Gemini CLI one declared check run in the shell is recorded. In Codex, trust the benchbook
hooks in `/hooks` first; a raw run through `exec_command` carries no exit code and is observed as `unrecordable`, so
check the ledger with `benchbook check run NAME`. After that observation, `benchbook doctor` shows
`harness hook input` (the line appears only once a hook has observed a declared check).
`benchbook uninstall --dry-run` lists the manifests it would rewrite without benchbook's entries. For OpenCode and Pi,
install the extension as its README says (`integrations/opencode/`, `integrations/pi/`) and check that the first
model request carries the brief.

## Upgrade from a pre-rename install

With the `assay@assay` plugin, `.assay/` and `ASSAY_*` variables in place, `benchbook doctor` lists `config folder`,
`data directory`, `state directory` and `legacy variables`, each with a command, and the commands in
[docs/claude-code.md](docs/claude-code.md#installs-from-before-the-rename-assay) work.

## Benchmark (real, paid)

```bash
uv run python -m bench.run --model sonnet --runs 3 --max-budget-usd 5                      # previews only
uv run python -m bench.run --model sonnet --runs 3 --max-budget-usd 5 --execute --claude-version <X.Y.Z>
uv run python -m bench.analyze_cost bench/out/<batch>   # writes bench/out/<batch>/cost-breakdown.md
```

`--claude-version` (in `bench.run`, `bench.boundary.run` and `bench.code.run`) stops a run before its first paid call
when `claude --version` differs; without it `--execute` only warns. The installed CLI updates itself, so also pass
`--claude-bin ~/.local/share/claude/versions/<X.Y.Z>` and follow bench/CLAUDE.md. `--runs 3` makes three runs of
each of A, B and C: nine runs at up to USD 5 each, USD 45 for the batch. The batch's `report.md` is in
`bench/out/<batch>/`. The compaction options of `bench.boundary.run` are in its `--help` and
[its preregistration](bench/boundary/PREREGISTRATION.md).

Release gate: C has zero lost results, a lower duplicate ratio and a lower peak lead context than A; report also
whether C meets the best-per-token target. Results go in [docs/results.md](docs/results.md).

## Release

Tagging `v0.1.0` is the owner's decision; follow [docs/releasing.md](docs/releasing.md), including its "Phase 0 exit
condition".

## Autoresearch (needs a GPU)

One-GPU smoke test:

```bash
git clone https://github.com/karpathy/autoresearch && cd autoresearch && uv run prepare.py
benchbook ar init --project smoke
export BENCHBOOK_AGENT=gpu0-smoke CUDA_VISIBLE_DEVICES=0
benchbook ar claim "baseline" && benchbook ar run -m "baseline"
```

Expected: `benchbook ar init` prints a partition like `h100-80gb/xxxxxxxx`; `run` takes about 5.5 minutes and prints
`KEEP (baseline, calibration pending)  val_bpb=0.99…`; `results.tsv` has the header and one `keep` row; `run.log`
holds the full output; `benchbook ar status` shows 1 run and 2 open calibration tasks. A second agent
(`BENCHBOOK_AGENT=gpu0-b`, a second worktree) runs `benchbook ar replicate --auto` in a temp directory without
touching that worktree's `train.py` or `git status`; after both calibration runs `benchbook ar status` shows
`calibration sigma=… epsilon=… (ok)` and a verified best. With an agent, `claude "Read program.benchbook.md and
start."` loops through replicate, sync, brief, claim, commit, run, keep/reset without asking questions.

The launch experiment (paid): fill in and commit
[bench/autoresearch/PREREGISTRATION.md](bench/autoresearch/PREREGISTRATION.md) first, check the pipeline with
`python -m bench.autoresearch.launch --sim`, then run each repetition `k` as it says:

```bash
python -m bench.autoresearch.launch --upstream ~/autoresearch --gpus-a 0,1,2,3 --gpus-b 4,5,6,7 --hours 8 \
    --model <dated model id> --max-budget-usd <cap> --gpu-hours-per-arm <cap> \
    --repetition k --assign-seed k --max-restarts 3                     # preview: prints commands.sh; --execute runs it
python -m bench.autoresearch.post_replicate bench/out/autoresearch-... --gpu 0 --execute   # metrics 1 and 5
python -m bench.autoresearch.report out1 out2 out3                      # summary.md over the repetitions
```

`post_replicate` without `--execute` prints how many training runs it needs (3 per arm plus 3 per sampled keep, about
5.5 GPU minutes each).