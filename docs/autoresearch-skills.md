# Autoresearch skills

A skill for the agent (pi-autoresearch, uditgoenka/autoresearch, codex-autoresearch) can run the autoresearch loop
instead of upstream's `program.md` ([autoresearch on many GPUs](autoresearch.md), scenario S5). benchbook does not
replace the skill's loop. It reads the log the skill already writes and records each run as an experiment, and the
gate applies to those runs as to runs started with `benchbook ar run`:

- **The gate is the referee's ([autoresearch.md](autoresearch.md#the-referee)).** `benchbook ar gate on` adds no
  mechanism: it makes sure the project uses the verified best as the baseline (`outcome_baseline = verified`), that
  the referee schedules `replicates` re-runs for each candidate, and that the ledger's rerun rule needs `rerun_min_seeds` runs from
  `rerun_min_authors` authors. `benchbook ar init` already sets these; `gate on` restores them when they were
  lowered and creates the project when a skill's checkout has none (without `program.benchbook.md`: the skill's
  own loop stays).
- **An imported run is a first run.** It is recorded with replication number 0, whoever imports it. A keep that
  beats the verified best becomes a candidate, and other agents re-run its `train.py` with
  `benchbook ar replicate --auto` before it can be verified. A skill that re-runs its own code does not add
  replications: the ledger already holds a first run of that code, so the second record is a duplicate.
- **Statuses map onto the existing ones.** A measured run is `ok`, a crash or an unparseable metric is
  `error`, and a run that the skill measured but must not count (a failed guard or check) is `invalid`. The
  ledger computes the outcome (`improved`, `no_change`, `worse`, `failed`) against the verified best, as for
  any run; the skill's own word is kept in `trace_ref.status` and the notes.
- **Code.** `train.py` comes from the commit the log names. pi-autoresearch and uditgoenka/autoresearch revert a
  discarded change without committing it, so their discards and crashes have no code: they are recorded with
  their value and description (others see the approach was tried) but as `invalid`, so a value nobody can re-run
  never becomes a candidate or the best that later runs are compared with. codex-autoresearch commits every trial
  before measuring it, so all its runs have code.
- **Evaluator.** Each run's partition is read from `prepare.py` at its commit, so the evaluator version is part
  of the run's identity (dedup key and comparison group) next to its `code_ref`, and is kept in
  `trace_ref.evaluator`.
- **Time budget.** `check_budget` runs on imported runs too: a run whose logged `training_seconds` exceeds the
  budget by more than the slack is `invalid`. No skill logs the wall clock, so only reported training time is
  checked; `gate on` prints whether the check is on (`time_budget_s` 0 turns it off).
- **Idempotent.** Every record has a key from its own fields (pi: run number, timestamp, commit, metric, status;
  uditgoenka: iteration, timestamp, commit, metric, status; codex: run id and sequence number). Importing the same
  log again, or another agent importing it, adds nothing (for the same `--hw`). `--hw` is required, as for results.tsv.
- Rows that measure another metric (a pi segment started for something else, a codex run with another metric
  name or direction) are skipped and counted; `--metric NAME` names the project's metric when the log calls it
  differently. An import is refused while the importing agent holds a replication task (the run would be
  recorded as that task's result).

The sample logs in `tests/fixtures/autoresearch/skills/` name the upstream commit each format was read from.

## pi-autoresearch

[davebcn87/pi-autoresearch](https://github.com/davebcn87/pi-autoresearch) (an extension for the pi agent) appends
every `log_experiment` to `.auto/log.jsonl` (older sessions: `autoresearch.jsonl`). Statuses: `keep` → ok,
`discard` → invalid (reverted without a commit, so no code), `crash` → error, `checks_failed` → invalid.

```bash
benchbook ar gate on --from pi-autoresearch      # once; add program.benchbook.pi-autoresearch.md to .auto/prompt.md
benchbook ar import --from pi-autoresearch .auto/log.jsonl --hw h100-80gb
```

## uditgoenka/autoresearch

[uditgoenka/autoresearch](https://github.com/uditgoenka/autoresearch) (`/autoresearch` for Claude Code and other
agents) appends one row per iteration to a results TSV in `autoresearch/loop-<YYMMDD>-<HHMM>/`. Statuses:
`baseline`, `keep`, `keep (reworked)` → ok; `discard` → invalid (reverted, the row names no commit);
`crash`, `metric-error` → error; `no-op`, `hook-blocked` and fork-and-flush summary
rows (`fork-<k>`, `fork-select`) are not runs and are skipped. Only the loop's TSV is accepted (other
subcommands write TSVs with other columns). The TSV records the metric's direction but not its name.

```bash
benchbook ar gate on --from uditgoenka           # once; add program.benchbook.uditgoenka.md to CLAUDE.md
benchbook ar import --from uditgoenka autoresearch/loop-261121-0930/ --hw h100-80gb
```

## codex-autoresearch

[leo-lilinxiao/codex-autoresearch](https://github.com/leo-lilinxiao/codex-autoresearch) (a Codex skill) keeps
`autoresearch-results/run.json` (metric name and direction) and `autoresearch-results/events.jsonl` (schema 2:
a baseline event, then one iteration event per trial). Outcomes: `keep` → ok, `discard` → ok (invalid when the
guard failed), `crash` → error. Other events (stopped, resumed, blocked, complete, error) are not runs.

```bash
benchbook ar gate on --from codex-autoresearch   # once; add program.benchbook.codex-autoresearch.md to AGENTS.md
benchbook ar import --from codex-autoresearch autoresearch-results --hw h100-80gb
```
