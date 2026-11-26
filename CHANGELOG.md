# Changelog

## Unreleased

The first release, 0.1.0. benchbook was developed as `assay`, whose names keep working (see "Upgrading from assay").
Subagents of a swarm write their experiments, check runs and completions to a Postgres ledger instead of the lead's
context, and the lead reads them back as a budgeted brief and by SQL; a reported "done" is re-run before it counts.

### Upgrading from assay

- Everything is named `benchbook`: package, command, `BENCHBOOK_*` variables, `.benchbook/`, schema `benchbook`, role
  `benchbook_reader`, MCP tools `benchbook_*`, plugin `benchbook@benchbook`, agent `benchbook-worker`,
  `program.benchbook.md`. Why: DECISIONS.md, "Name".
- The `assay` command works during 0.1.x (one notice, then `benchbook`; deployed `assay hook` hooks write it to
  `hook.log`). `ASSAY_*` variables are read when the `BENCHBOOK_*` one is unset.
- `.assay/config.toml`, the old embedded cluster and `~/.local/state/assay` stay in use while no new one exists;
  `benchbook doctor` shows how to move each. `assay://blob/` artifacts, reports queued by an `assay` build and the old
  plugin (`assay@assay`, `assay-worker`, `assay_*` tools) keep working.
- `benchbook migrate` or the first connection renames an `assay` schema and role in place, in one transaction (`migrate
  --json`: `renamed_from`). A database holding both schemas is refused; without the privilege to rename, the message
  prints the SQL for an administrator.

### Ledger and SDK

- Embedded PostgreSQL 16 whose server outlives its starter, or any server through `BENCHBOOK_DATABASE_URL`. Alembic
  revisions 0001-0009 of plain SQL: append-only and decision-role triggers, NOTIFY (a hashed channel for project ids
  over 53 bytes), read views (`v_partition_best`, `v_lineage`, `v_inflight`, `v_knob_history` among them),
  `benchbook_reader`.
- `benchbook.Ledger`: experiments, claims linked to evidence, decisions, tasks with leases, worker intents, partitions
  with per-partition epsilon and a verified-best baseline, audited settings, content-addressed blobs (`benchbook blob
  put/get`). Invalidated experiments stay out of briefs and claims resting on them are flagged.
- pack.toml `dedup_mode`: `"refuse"` (default) refuses a run whose config, code_ref and seed were recorded, invalidated
  runs included; `"notify"` stores it with `duplicate_of` and a notice (`exp record` exits 0).
- pack.toml `[verify] rule`: `"replication"` (independent re-runs compared against a baseline) or `"all_pass"` with
  `min_runs` / `min_authors` (code pack). `Ledger.check_rerun` dry-runs it; `Ledger.completion_attempts` lists a
  completion's attempts.
- Per-agent tokens: `benchbook agent add NAME --role worker` prints one (only its hash is stored); `--rotate`, `agent
  revoke`, `agent list`; clients read `BENCHBOOK_TOKEN`. On the `benchbook_writer` login every write carries its agent's
  token, decisions and settings need an orchestrator or human, a worker's intent needs `settings.worker_intents`, and
  leases are capped by `settings.max_lease_seconds`. Writes need a named agent; reads fall back to `human`.
- `heartbeat` and `update_intent` refuse a lease that ran out (`LeaseLost`); `exclusive_paths` are checked for overlap
  under the intent lock (`benchbook.core.globs`).
- `benchbook.core.runner.run_command` (`metric=None`, `tee=True`) waits for the whole process group after a timeout.
- `benchbook sql` and the MCP SQL tool read through the restricted `benchbook_sql` with bounded output.
- Search lines end with their tags; `--all-tags` keeps records with every listed tag.
- Package layout: `core`, `checks`, `code`, `hooks`, `cli` (DECISIONS.md, "Package layout"); `from benchbook import
  Ledger`; `python -m benchbook`.

### Command line

- `benchbook` with `--json` and exit codes 0/1/2/3: `init`, `doctor`, `migrate`, `status`, `statusline`, `brief`, `get`,
  `search`, `link`, `decide`, `discard`, `sql`, `export`, `exp`, `task`, `claim`, `agent`, `check`, `piece`, `finding`,
  `pr`, `blob`, `db`, `ar`, `install`, `uninstall`, `hook`, `mcp`, `serve`, `ui`, `--version`.
- `benchbook exp run --config '<json>' [--seed N] -- <cmd>` checks the ledger, runs the command and records the result;
  a duplicate is not run (exit 2), `--retry-failed` re-runs a config that only failed, and the exit code passes through.
- `benchbook doctor` prints the build and schema revision and checks the Claude Code connection (CLI on its PATH,
  plugins current, environment equal to the shell's, hook failures, stuck or dead reports, each harness's hook input);
  each failure prints the command that fixes it.
- `benchbook get <id>` prints at most 400 tokens (status, `checked:` label, links, last decisions); lookups go to
  `lookups.jsonl`. `benchbook task claim <id>` claims one task.
- `benchbook install [--harness claude-code|codex|cursor|gemini] [--mcp] [--no-cli] [--dry-run]` (the CLI from PyPI for
  a release, from the git commit and subdirectory a development build was installed from, or left as it is when that is unknown) and `benchbook uninstall [--purge] [--dry-run]`; uninstall keeps `.benchbook/` when git cannot say whether it is tracked, and `--purge` removes only a
  cluster `benchbook db stop` confirmed stopped.
- `benchbook mcp` (stdio) and `benchbook serve` (HTTP at `/mcp`, a token for non-loopback binds) with worker and
  orchestrator tool sets and `benchbook://schema`; it starts before the project is initialized, and a worktree finds the
  main checkout's config. `benchbook ui` is a read-only live viewer: best-so-far curve, verified-best staircase, claims
  board, tasks, in-flight work, lineage and knob pages, `train.py` diffs, a partition selector.

### Code pack: verified completion

- `benchbook init` without `--metric` and `--pack` makes a code project: it proposes the test and lint commands it finds
  as `[checks.<name>]` with `[guard]` paths and base, pins them, excludes `.benchbook/` from git, writes `[report]
  agents = ["*"]` and `worker_intents = true`, and ends with the next commands. `--yes` does not ask.
- `benchbook check verify <piece> <commit>` re-runs the checks declared at the merge-base (or pinned with `check pin` or
  `piece start --check`) on the commit in a clean worktree, without the ledger's credentials, and records the completion
  claim and the attempt linked supports or refutes. Options: `--reported-by`, `--claim`, `--base`, `--checks`,
  `--trace-ref`, `--no-decide`. Under `BENCHBOOK_TOKEN` the token's agent is the verifier. `[verify] copy` and `prepare`
  set up the worktree (a failed preparation is an environment failure); `parallel_baseline` runs the merge-base's JUnit
  checks beside the commit's.
- With lead authority verify decides `verified` (every check passed, no test weakened, `[checks]` unchanged) or
  `inconclusive` (attempts disagree); otherwise it records and prints the command that decides. Runs from the
  implementer's checkout, partial runs and runs with uncommitted changes never verify.
- A `junit = true` check that exits 0 fails unless tests ran and none failed. Tests added are not tests weakened:
  deleted or changed lines (adding an import name excepted), skip markers, conftest.py changes and fewer JUnit tests
  than at the merge-base are (`guard_weakened`).
- A check that fails the same way at the merge-base, after an implementer passed it, is an environment failure: no
  refutation and no discarded approach.
- Failure signatures (first failing test, TypeScript codes, or exit code and last-line hash; never the output). A failed
  attempt leaves a discarded approach, and a later run that fails the same way names it. `check run --approach NAME` or
  `BENCHBOOK_APPROACH` names the approach; a lead records its own with `benchbook discard <piece> --approach NAME --why
  "..."`.
- `benchbook check run <name>` runs a declared check from the checkout with its own output and exit code and records it
  through the hook spool.
- Path occupancy: `benchbook piece start <piece> --paths 'glob'...` refuses paths a live piece holds (exit 2); `piece
  done` releases them, `piece list` shows who holds what, and starting again renews the lease or restores an expired
  piece's paths.
- Out-of-scope findings are recorded from an implementer's report (`[report] out_of_scope_heading` or a `범위 밖:` line),
  from the reports of the subagent types in `[report] agents`, and by `benchbook finding add "..." [--file PATH]`;
  `finding accept <claim> --issue URL --oracle human --as human` accepts one, and
  `decide <claim> refuted --oracle human --rationale "..." --as human` rejects it.
- [docs/code-brief.md](docs/code-brief.md#record-coverage) has the queries for record coverage (`observed.jsonl`), and
  docs/code.md the one for the false completion rate.

### Hooks and plugins

- Claude Code plugins `benchbook` 0.2.5 (hooks only) and `benchbook-mcp` 0.2.2 (the MCP server, the `benchbook-worker`
  subagent and the lead skill `/benchbook-mcp:benchbook-lead`). Manual install files and an AGENTS.md snippet cover
  other setups. Worker instructions use `exp run` where a shell exists.
- `hook subagent-stop` captures the types in `BENCHBOOK_HOOK_AGENTS` (default `benchbook-worker`) and confirms cited ids
  against write receipts; compaction's SubagentStop is never captured. `hook bash-result` records a declared check run
  (exit code and last-line hash, never the output) and returns within 50 ms for any other command; a raw Bash JUnit pass
  is observed only.
- The completion gate (`hook task-completed`) keeps a task open on a live piece's branch (or with `BENCHBOOK_PIECE`)
  until HEAD passes every declared check.
- The merge gate (`hook bash-start` on `git merge`) holds a merge once when the reported commit's re-run did not pass
  cleanly within 30 s, with the command to run; merging again goes through.
- A `DONE <sha>` report (PostToolUse on Agent/Task or SubagentStop) is re-run once as `rerun@<host>`, a failed run
  retried once (runs that disagree are inconclusive); the verdict reaches only the lead, at its next hook that can give
  context.
- `session-start` gives the branch's brief and writes `.benchbook/STATE.md`, the fallback when the ledger does not
  answer; a new code-project session gets a short guide for a lead. `pre-compact` gives ids and statuses to keep.
- The hook spool has a lane for re-runs and one for the rest, one directory per token; `benchbook hook replay` replays
  both, adopts a rotated token's entries and reports unconfirmed ones as `held`.
- Codex, Cursor and Gemini CLI manifests, and OpenCode V2 and Pi extensions, record checks, capture reports, inject the
  brief and deliver notes (docs/harnesses.md).

### Brief and status

- A deterministic brief within a token budget (`Ledger.brief`, `compile_brief`), footer `benchbook get <id>`; a task's
  brief shows the unjudged findings under its tags.
- The code brief: verified pieces marked `applies at HEAD`, `changed since (N commits)` or `not merged into this
  branch`; "Re-run, not decided" with why; "Reported done, not re-run"; discarded approaches; findings; occupied paths
  (`test changed since`, `commit not in this checkout`; pack.toml `[brief] rerun` and `[brief.labels]`). `brief --here`
  scopes it to the branch and the pieces merged into it, `--tags` (MCP `benchbook_brief(tags=...)`) to tags.
- `benchbook status` shows piece outcomes, open findings, hook queue health and recording failures (`--json`:
  `pieces.rerun_undecided`); `benchbook statusline` prints a cached one-line summary.

### CI

- The GitHub Action (`integrations/github-action`) re-runs each piece a `Benchbook-Piece:` trailer names on a pull
  request, with a shared ledger or a ledger of that run only (the head as `pr-<number>`), with the run's URL in
  `trace_ref`, and keeps one summary comment; fork and Dependabot pull requests get a notice and pass. `benchbook pr
  pieces [--claims]`, `pr summary` (both `--whole-as NAME`) and `pr comment` do the same by hand.
- The PR summary's notes on the states and `check verify --help` say that a weakened test or a changed `[checks]`
  sends a piece to a person; a commit that only adds tests does not.

### Experiment swarms and autoresearch

- The `ml-research` pack and `benchbook ar` (`init`, `brief`, `claim`, `run`, `replicate`, `sync`, `note`, `idea`,
  `status`, `referee`), `program.benchbook.md` and `launch_local.sh`; the deterministic referee calibrates and asks
  distinct agents for replications against a verified baseline.
- `benchbook ar import --from pi-autoresearch|uditgoenka|codex-autoresearch <path>` records those skills' logs and makes
  their keeps candidates that others re-run; `benchbook ar gate on [--from <skill>]` turns on the reproduction gate in
  one command.
- A CPU simulator with scripted agents, and the circle-packing demo with an LLM-free swarm.

### Benchmarks and measurements

- `bench`: arms A baseline, B files and C benchbook, with `--dry`, cost breakdowns and usage reconciliation; the
  autoresearch launch harness; a circle-packing worker-count sweep.
- SWE-bench Verified: 7 of 39 completions failed an independent re-run, 17.9% (95% CI 9.0% to 32.7%).
- Fan-in at 8, 16 and 30 workers (H5) did not meet its pre-registered gate.
- `bench.boundary`: a six-arm boundary benchmark (H1-H6, a model-free dry run, `--force-compact`, `--compact-pct`) and
  its pilot. `scripts/qa_boundary_hooks.sh` checks on real sessions what the hooks rely on;
  `scripts/measure_rerun_queue.py` times the spool.
- Results are in bench/results/ and docs/results.md.

### Project

- Python 3.11 to 3.13 on Linux and macOS; CI tests 3.11 and 3.13 on Linux and 3.13 on macOS. The version comes from git;
  a `v*` tag publishes to PyPI with Trusted Publishing after a wheel smoke test on both systems (docs/releasing.md).
