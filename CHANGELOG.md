# Changelog

## Unreleased: code pack

- Codex, Cursor and Gemini CLI hooks inject the scoped startup brief and deliver notes after tools. OpenCode V2
  and Pi support the same boundary context and shell recording through project extensions.
- Added a six-arm boundary benchmark with a model-free dry run and H1–H6 reporting. Circle-packing accepts a
  worker-count sweep and reports delivered subagent reports and estimated report tokens.
- Code project status shows piece outcomes, open findings and hook queue health. The brief surfaces recording
  failures, and `benchbook statusline` reads a compact summary cached by the spool processor.
- Claude Code's Agent/Task result hook queues an independent check run from a completion report. The next
  supported hook in that session delivers its result once, with the completion ID and any review requirement.
- Boundary hooks in the `benchbook` plugin: `benchbook hook session-start` (SessionStart) gives the branch's brief
  (1,500 tokens, 6,000 characters) as additionalContext and writes it to `.benchbook/STATE.md`, which it gives instead
  when the ledger does not answer within 5 seconds; `pre-compact` (PreCompact) gives a 1,000-character paragraph of
  ids and statuses to keep in the compaction summary; `notes` (UserPromptSubmit) delivers notes queued for the
  session once. `harnesses.context_answer` holds each harness's answer form. docs/claude-code.md, "The boundary hooks".
- `scripts/qa_boundary_hooks.sh` (MANUAL_QA.md, scenario 5) checks on real sessions what the boundary hooks rely on:
  SessionStart sources and injected context before and after compaction, PreCompact's input and output, PostToolUse
  on the Agent tool (foreground and background), repeated `--plugin-dir` and the `benchbook-mcp` tool prefix.
  `--save-fixtures` replaces the documented fixtures in `tests/fixtures/hooks/` with what it saw.
- The code brief puts verified completions first, then a new section "Reported done, not re-run" (proposed
  completions with no evidence, `checked: none`), then discarded approaches, findings and occupied paths. Verified
  lines say how they were checked (`checked: re-run`, `checked: person`). `brief --here` marks a verified piece whose
  paths changed since its commit (`changed since (N commits)`, `commit not in this checkout`) and a discarded
  approach whose test file changed since (`test changed since`, moved to the end); computed when read, never stored.
  `Ledger.brief` and `compile_brief` take `repo` and `max_chars`; the footer points to `benchbook get <id>`.
- Two Claude Code plugins: `benchbook` (`integrations/claude-code/`, hooks only, plugin 0.2.0) and `benchbook-mcp`
  (`integrations/claude-code-mcp/`: the MCP server, the worker subagent, the skills; the lead skill is now
  `/benchbook-mcp:benchbook-lead` and the worker's tools `mcp__plugin_benchbook-mcp_benchbook__*`). Every hook runs
  `"${CLAUDE_PLUGIN_ROOT}/bin/benchbook-hook" EVENT`, a shim that notes a missing CLI in `missing-cli` (reported
  by `doctor`) instead of failing.
- `benchbook install [--harness claude-code|codex|cursor] [--mcp] [--dry-run]` and `benchbook uninstall [--purge]
  [--dry-run]`. The wheel ships the Codex and Cursor manifests for install to merge.
- `benchbook init` without `--metric` and `--pack` makes a code project: it proposes the test and lint commands
  found in pyproject.toml, package.json, a Makefile, go.mod or Cargo.toml as `[checks]` with guard paths and the
  base branch (asking on a terminal; `--yes` does not ask).
- `benchbook get <id>` prints at most 400 tokens of text (status, `checked:` label, statement, five links each way, the
  last three decisions, cut items counted); `--json` is unchanged. Lookups are logged to `lookups.jsonl` in the
  state directory. Pack.toml `[brief.labels]` names the oracles ("re-run", "person", ...).
- `benchbook brief --here` scopes the code brief by what the current branch changed since its base (`file:<dir>/`
  prefixes, at most 20, and `piece:<branch>`). Discarded approaches and completion claims carry the directories their
  commit changed (`payload.paths`, `file:` tags; the spool's check attempts too), and the brief finds discarded
  approaches and verified completions by those tags, without a `piece start`.
- Out-of-scope findings in one line: `범위 밖: ...` (also as a list item, in bold, with a full-width colon) is a
  finding. `[report] agents` (subagent types or `["*"]`) reads those subagents' reports for findings only, with no
  `[auto-captured]` note, while `BENCHBOOK_HOOK_AGENTS` is unset. `init --pack code` writes a `[report]` table
  with `agents = ["*"]`.
- Environment failures: a failing check that the implementer's own clean run of the commit passed (`via`) is run
  again at the merge-base (cached per merge-base and check definition in the state directory). When every failing
  check fails the same way there, the attempt is an environment failure: `run_status` error,
  `metrics.environment` and `baseline_signature`, no refutes link, no discarded approach.
- Tests added are not tests weakened: guarded files are split into added (new files, lines only added) and weakened
  (lines deleted or changed, skip markers, any conftest.py change), and fewer JUnit testcases than at the
  merge-base is a weakening. `guard_weakened` (plus `guard_weakened_files`, `guard_added_files`, `tests_before`,
  `tests_after`) replaces `guard_touched` in the all_pass rule, check verify's decision and the PR summary, which
  now says "added" and "weakened" apart. Older attempts are read by `guard_touched`.
- `[verify] copy` and `prepare` (config.toml or the pin) prepare check verify's clean worktree: gitignored files are
  copied from the repository root (names recorded, never content) and a command runs first. A failed preparation
  runs no check and is recorded as an environment failure (`run_status` error, `metrics.environment`), with no link
  and no discarded approach.
- Checks pinned in the ledger: `benchbook check pin [PIECE] [--check NAME=CMD]` and `piece start --check` record the
  checks `check verify` runs when the merge-base has no committed config.toml (a `note` claim tagged `checks-pin`).
  Pins by the piece's implementer are ignored; a pin that changed under the work sets `checks_changed`. The attempt
  carries `checks_source`, `checks_pin` and `checks_sha256`. `init --pack code` adds `.benchbook/` to
  `.git/info/exclude` when git does not track it, pins the declared checks, and keeps an existing config.toml's
  tables (it rewrites only the `project` line). docs/code.md, "Checks pinned in the ledger".
- `benchbook check verify <piece> <commit> [--reported-by NAME] [--report-file F] [--base REF] [--checks a,b]`
  re-runs the checks declared in `.benchbook/config.toml` `[checks]` on a reported commit in a clean worktree. It
  records the completion claim (the implementer's) and the attempt (the verifier's), linked supports/refutes, and
  notes guarded files and `[checks]` changes. See docs/code.md.
- New `code` pack and `benchbook init --pack`. `--pack code` defaults to metric `checks_passed` (max),
  partitioned by piece.
- `.benchbook/config.toml` accepts `[checks.<name>]` (cmd, junit, timeout) and `[guard]` (paths, base).
- `benchbook check verify` decides when the verifier has lead authority (orchestrator or human): `verified`
  (oracle rerun) when every check passed, nothing guarded was touched and `[checks]` is unchanged; `inconclusive`
  when earlier attempts of the piece at that commit disagree, with both ids as evidence; no decision when guarded
  files or `[checks]` changed, with the files named. Without lead authority it records as before and says so.
  `--json` adds `decision`, `decision_reason`, `needs_person`, `evidence`, `decisions`.
- pack.toml `[verify]`: `rule = "replication"` (default, the rule so far; core and ml-research) or `"all_pass"`
  with `min_runs` / `min_authors` (code pack: 1 and 2). Under `all_pass`, `decide ... verified --oracle rerun` on
  a completion needs clean passing re-runs by agents other than the implementer and no failed or guard-flagged
  attempt of that commit; otherwise it is refused, naming the `human` oracle.
- SDK: `Ledger.check_rerun(claim, evidence)` (dry run of the rerun rule) and `Ledger.completion_attempts(claim)`.
- Under `BENCHBOOK_TOKEN`, `check verify` uses the token's agent as the verifier and records the completion claim
  in its own name, with `payload.reported_by`.
- `benchbook piece start <piece> --paths 'glob'... [--tags mod:x] [--lease S]` declares the paths a piece will
  change. A piece whose paths overlap a live piece's (same file, a file under its glob, or two globs that can
  match one path) is refused with the first piece's task id (exit 2). `piece done` releases it, `piece list`
  shows who holds what, and starting again renews the lease. See docs/code.md, "Path occupancy".
- SDK: `create_intent` and `update_intent` take `exclusive_paths`, a spec field holding path globs checked for
  overlap under the intent lock. `benchbook.globs` gains `overlap`, `first_overlap` and `escape`.
- `init --pack code` also sets `worker_intents = true`.
- Out-of-scope findings: with `[report] out_of_scope_heading` in `.benchbook/config.toml`, the hook records each
  item of that section of a code-pack implementer's final report as an `observation` claim tagged `out-of-scope`
  (plus `piece:`, `mod:`, `file:` when known). A repeated finding adds the new report as evidence instead of a
  second claim (docs/code.md).
- `benchbook finding accept <claim> --issue URL --oracle ...` accepts a finding: it links the follow-up issue as a
  source and decides it `supported`. Reject with `benchbook decide <claim> refuted ...`.
- `benchbook check run <name> [--piece P]` runs a declared check from the current checkout. The output and exit
  code are the command's, and the run is recorded through the hook spool as an attempt in check verify's shape
  plus `via` and `worktree_dirty`. A database outage never changes the exit code; `benchbook hook replay`
  records the run later.
- Hooks: `benchbook hook bash-result` (PostToolUse/PostToolUseFailure on Bash) records a declared check run as an
  attempt, with exit code and last-line hash, never the output. `benchbook hook task-completed` (TaskCompleted)
  keeps a piece's task open until its HEAD passes every declared check. Plugin 0.1.3; `benchbook doctor` flags
  older installs.
- Record coverage: matched runs are logged to `observed.jsonl` in the spool directory; the query is in
  docs/code.md.
- Runs from the implementer's checkout (`via`) never count as independent re-runs, and a run with uncommitted
  changes is no attempt of its commit.
- `bench/code/`: SWE-bench Verified measurement of the false completion rate: a pre-registration, 40 committed
  instances (`select.py`), the visible and hidden checks in the harness's Docker image (`swe_check.py`), a
  resumable implementer + `check verify` runner with `--dry` (`run.py`), and a report whose rate comes from
  `docs/code.md`'s SQL (`report.py`). Not yet run.
- `bench`: no arm loads the account's claude.ai connectors any more (`ENABLE_CLAUDEAI_MCP_SERVERS=false`);
  `command.json` records the MCP servers a session started with.
- Codex CLI and Cursor hook manifests (`integrations/codex/hooks.json`, `integrations/cursor/hooks.json`) run
  `benchbook hook bash-result` and `benchbook hook subagent-stop` with `--harness codex|cursor`. Declared check
  runs are recorded when the harness's input gives the exit code (Cursor `tool_output.exitCode`; Codex only when
  `tool_response` carries its `Process exited with code N` header) and observed as `unrecordable` otherwise.
  Worker reports are captured as notes. See docs/harnesses.md.
- `benchbook hook ... --harness gemini` reads Gemini CLI's `AfterTool` result for `run_shell_command` (the
  `Exit Code: N` line). No manifest.
- `observed.jsonl` lines carry `harness` (and `harness_version` from Cursor). `benchbook doctor` adds
  `harness hook input`, which fails when a harness's hook input no longer has the documented shape.
- Failure signatures come from one module (`signature.py`): the first failing JUnit test and its type (plus a
  count and set hash when several fail), TypeScript error codes and files, or the exit code and a hash of the
  normalised last error line. `check verify`, `check run` and the Bash hook use it. The output is never stored.
- `check run` and `check verify` name an earlier attempt of the same piece that failed the same way, and the
  approaches tried. It is a notice, not a refusal. `check run` asks only after a failure and waits at most 2 s.
- A finished failed attempt leaves a discarded-approach claim (`observation`, tags `discarded`, `piece:<p>`,
  `approach:<a>`, `mod:`), one per piece, signature and approach. `check run --approach NAME` /
  `BENCHBOOK_APPROACH` sets `config.approach`.
- Code projects get their own brief: `benchbook brief --tags mod:media` (MCP `benchbook_brief(tags=...)`) shows
  discarded approaches, open out-of-scope findings, occupied paths and the last verified commit per piece within
  the budget. Other packs refuse `--tags`. docs/code.md also has the "reads that changed a plan" count.
- GitHub Action `integrations/github-action`: on a pull request it runs `check verify` for each piece named by a
  `Benchbook-Piece:` commit trailer, as `ci:<run_id>` (or the token's agent), with the run's URL in `trace_ref`.
  It keeps one comment marked `<!-- benchbook-summary -->` with each piece's state (verified, claimed only, re-run
  disagrees, not reported), discarded approaches, open out-of-scope findings and test changes. Fork and Dependabot
  pull requests get a notice and pass. See docs/code.md, "CI evidence and the PR summary".
- `benchbook pr pieces --base B [--head H] [--claims]`, `benchbook pr summary --base B [--head H] [--run-url URL]`,
  `benchbook pr comment --repo O/N --pr N --body-file F [--author LOGIN] [--update-only]`.
- `benchbook check verify` takes `--trace-ref JSON|@file` (stored on the attempt) and `--no-decide` (record only,
  even with lead authority). SDK: `checks.verify(..., trace_ref=, decide=)`.
- check verify finds a completion claim by piece, commit and reporter, not by its statement: another agent's claim
  with the same statement no longer takes the credit or blocks the piece; such a report gets a claim whose
  statement names the reporter.
- The implementers of a completion include the reporter of every completion claim of the same piece and commit;
  `pr pieces --claims` and the PR summary take `payload.reported_by` only from claims a lead, a person or a
  harness agent recorded.
- A check with `junit = true` that exits 0 now fails unless its JUnit report shows at least one test ran and none
  failed (`exit 0, no JUnit report`, `exit 0, no tests ran`), in check verify and check run.
- check verify runs the checks without the ledger's credentials (`BENCHBOOK_TOKEN`, `BENCHBOOK_DATABASE_URL`,
  `BENCHBOOK_READER_PASSWORD`, their `ASSAY_` forms, `PGPASSWORD`, `PGPASSFILE`).
- Under tokens the database refuses a worker's intent without `settings.worker_intents`, a lease longer than
  `settings.max_lease_seconds` (new, default 86400), and an `expired` task event that does not name the previous
  holder.
- check verify records `metrics.checks_declared` (every check declared at the merge-base). A run of only some
  checks (`--checks`) is recorded as partial: it never verifies a completion, in the all_pass rule, in verify's own
  decision or in the PR summary, though a check it ran that failed still counts.
- check verify takes `--claim ID`: the re-run is evidence for that completion claim, after checking that it is
  about the piece and commit; its reporter is the implementer. `pr pieces --claims` adds the claim id as a fourth
  column and the GitHub Action passes it, so CI links its re-run to the claim the summary shows. Without `--claim`,
  verify also finds a claim whatever its statement (by piece, commit and reporter).
- The hook spool is bound to its writer: under `BENCHBOOK_TOKEN` each token has its own spool directory, and an
  entry records a digest of the token it was queued with. A processor or `hook replay` under another token (or
  none) leaves it queued, so one agent can no longer record another's queued reports or runs in its own name.
  Without a token the spool is where it was.
- A passing `junit = true` check needs an invocation-specific report from `benchbook check run` or
  `benchbook check verify`. The Bash hook observes a raw exit 0 and records explicit failures. The completion
  gate also excludes legacy raw Bash JUnit passes from its evidence. Claude Code plugin 0.1.5 drops the
  `bash-start` hook. The command remains available for older manifests.
- `heartbeat` and `update_intent` refuse a lease that has run out (`LeaseLost`), so an expired piece is never
  revived next to the piece that took its paths. `piece start` without `--paths` after its lease ran out takes the
  same paths again as a new piece, through the overlap check.
- After a timeout or an interruption the runner waits for the command's whole process group, not only the shell,
  and sends SIGKILL to what is left after 10 s: a child that ignores SIGTERM no longer outlives the run.
- `heartbeat` takes the intent lock the overlap check holds and reads the expiry after it, so a renewal can no
  longer commit next to a piece that took the paths meanwhile.
- A partial pass gets no `supports` link: it no longer counts as a re-run in the false completion rate. A partial
  run that fails still refutes.
- `piece start` without `--paths` also restores a piece that a `next_task` marked failed when its lease ran out;
  one released with `piece done` still needs `--paths`.
- `hook replay` under an agent's new token (after `agent add --rotate`) adopts the entries queued under its
  earlier token, when a processor had confirmed that token's agent (`writer.info` in the spool).
- The Bash hook matches a check declared with a bare `--junitxml {junit}`: the word before the option was lost.
- A new piece and a renewal count their lease from the database's clock after the intent lock, and the overlap
  checks read expiry with the same clock.
- Piece start, renewal, and release use one project transaction for state lookup and mutation. After explicit
  release, a fresh start requires `--paths`.
- `hook replay` reports entries queued under a token no processor ever confirmed as `held` and exits nonzero.
  Queued checks reuse their experiment ID across retries and token rotations. A retry after a committed write
  returns the existing experiment, including when local queue cleanup failed.

## Unreleased: core for the verification ledger

- `benchbook exp run --config '<json>' [--seed N] -- <cmd>` checks the ledger, runs the command (output shown
  unchanged) and records the result in one command. Duplicates in a refuse-mode pack are not run (exit 2);
  `--retry-failed` re-runs a config that only failed. Metrics come from `key: number` lines or a JSON last line.
  Non-zero exits, timeouts and interrupts are recorded; the exit code passes through.
- Worker instructions (plugin, manual copy, skill, AGENTS.md) use `exp run` where a shell is available; the MCP
  tools remain the fallback. Plugin 0.1.2. The capture hook recognises `exp run` receipts.
- circle-packing: `run_config.py` scores one `packing.py` config; README (b) uses `exp run`.
- `bench`: real runs save the Claude Code session, copy the lead's and subagents' transcripts into the run
  directory, and record per-run usage reconciliation (`usage_check`) in `metrics.json`. Configuration C also
  keeps the queued hook entries and a `benchbook export` of its project.
- `bench`: new `python -m bench.analyze_cost <runs>` writes `cost-breakdown.md`: prompt tokens by what entered
  the context, repeated and near evaluations, and a verdict for each cost hypothesis.
- Core (migration 0009): `dedup_mode = "refuse" | "notify"` in pack.toml. `refuse` (default; core and ml-research)
  is the behaviour so far. With `notify`, `record_experiment` stores an ok run whose config, code_ref and seed
  were already recorded and returns the first such run as `duplicate_of` with a `notice`; the MCP tool returns
  both, `benchbook exp record` exits 0 (notice in `--json`, on stderr otherwise). `experiments.duplicate_of`
  marks the repeat, `dedup_hits` logs it as `notified`, and the unique indexes still refuse any duplicate that
  does not declare its original.
- Hook: a recorded repeat (new id with `duplicate_of`) counts as the subagent's own write.
- Per-agent tokens: `benchbook agent add NAME --role worker` prints a token once (only its SHA-256 is stored);
  `--rotate`, `agent revoke` and `agent list` manage it. Clients read `BENCHBOOK_TOKEN`.
- Migration 0008: the `benchbook_writer` login. On it, every write must carry the token of the agent it names,
  and decisions, settings, open tasks and cancellations need an orchestrator or human token. Owner connections
  (local mode) are unchanged. `export()` no longer includes agent token hashes.
- Runner: the command runner is core (`benchbook.runner`). `run_command` takes `metric=None` (exit code 0 alone
  is ok) and `tee=True` (the output goes to the log file and, unchanged and as it arrives, to stdout). The
  autoresearch runner module re-exports the same names.
- `benchbook ar import --from pi-autoresearch|uditgoenka|codex-autoresearch <path> --hw <class>` records the run
  logs of the three autoresearch skills as experiments. Imported keeps become candidates that other agents
  re-run. Importing the same log again adds nothing.
- `benchbook ar gate on [--from <skill>]` turns on the reproduction gate (verified baseline, referee
  replications, rerun rule) in one command, creating the project if needed, and writes a program fragment for
  the skill. Imported runs, results.tsv included, now get the time-budget check on the training time the log
  reports.

## Unreleased: renamed to benchbook

- The project is now `benchbook`: distribution, import package, command, `BENCHBOOK_*` environment variables,
  `.benchbook/config.toml`, database schema `benchbook` / `benchbook_q`, role `benchbook_reader`, MCP tools
  `benchbook_*`, plugin `benchbook@benchbook`, agent `benchbook-worker`, skills `benchbook` and
  `benchbook-lead`, `program.benchbook.md`. Why: DECISIONS.md, "Name".
- The `assay` command still works during 0.1.x: it prints one notice and runs `benchbook`. Deployed
  `assay hook ...` hooks stay silent and write the notice to `hook.log`.
- `ASSAY_*` variables are read when the `BENCHBOOK_*` one is unset, with one notice per process.
- `.assay/config.toml`, the old embedded cluster (`~/.local/share/assay/pg`, or
  `~/Library/Application Support/assay/pg` on macOS) with its `assay` database, and `~/.local/state/assay` stay
  in use while no new one exists. `benchbook doctor` shows how to move each one; nothing is moved for you.
- Records with `assay://blob/` artifacts, reports queued by the old build, and the old plugin (`assay@assay`,
  `assay-worker`, `assay_*` tools) keep working.
- A database made before the rename (schema `assay`, role `assay_reader`) is renamed in place by
  `benchbook migrate` or the first connection, in one transaction. Functions and reader views are rebuilt, and
  stored `search_path` settings are rewritten. Old rows keep their `assay://blob/...` URIs. `benchbook doctor`
  reports a pre-rename schema; `benchbook migrate --json` includes `renamed_from`. A database holding both the
  `assay` and `benchbook` schemas is refused, and the message says which one to drop. When the owner may not
  rename or create roles, nothing changes and the message prints the SQL for an administrator.
- Project ids longer than 53 bytes (was 57) use the hashed notification channel name.
- Release: `.github/workflows/release.yml` publishes to PyPI from a `v*` tag with Trusted Publishing; CI builds the
  wheel and runs `uvx --from dist/*.whl benchbook doctor` and `benchbook init` on empty Linux and macOS runners
  (`scripts/smoke_wheel.sh`). The checklist is docs/releasing.md.

## Unreleased: release readiness

- Build identity: the package version comes from git (`0.1.devN+g<commit>` between tags, `0.1.0` on `v0.1.0`);
  `assay --version`; `assay doctor` prints the build, the database schema revision and this build's newest one.
- `assay doctor` checks the Claude Code connection: `assay` on Claude Code's PATH, plugin `assay@assay` installed
  and enabled, `ASSAY_DATA_DIR`/`ASSAY_PROJECT` in Claude Code settings equal to the shell, recent `hook.log`
  failures and `dead/` reports. Each failure prints the command that fixes it and the exit code is 1.
- Search: every line ends with the record's tags; `--all-tags` (CLI), `all_tags` (SDK, MCP `assay_search`)
  keeps records that have every listed tag (`--tags` keeps those with any).
- Hook: the plugin's `SubagentStop` hook fires for every subagent and `ASSAY_HOOK_AGENTS` (default
  `assay-worker`) picks the captured types; other types exit before reading config or spooling (about 150 ms
  through the console script, which now skips the CLI framework for `assay hook`). Plugin version 0.1.1, so
  existing installs pick up the new hook with `claude plugin update assay@assay`; `assay doctor` flags an
  install that still has the old matcher.
- `assay doctor` also compares `ASSAY_DATABASE_URL` between the shell and Claude Code (by server and database, never
  printed), reads hook failures per database spool (a replay of another database's spool no longer hides them,
  and reports queued for over 5 minutes count), and keeps going when one check raises (a bad `~user` path).
- Hook replay: a malformed entry from an earlier build (`tool_input` not an object) no longer stops the
  entries after it.
- README: what assay fits (a number per attempt, re-runnable, one space explored together) and why delegating
  code implementation is not one of them.
- Benchmark: subagents run in the foreground (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`); newer Claude Code started
  them in the background and the lead ended before its workers reported.

## Unreleased: review fixes (assay-review.md)

- Core (migration 0006): the rerun oracle compares values against a baseline; TRUNCATE and agent re-roling are
  refused; `assay_sql` cannot call `set_config` and rejects `U&` escapes and `*_to_xml`; seed NULL and -1 differ;
  `v_tried` summarises replicated configs by their mean; candidates no longer need seed 0; `check_tried` reports
  invalidated runs; nested `dedup_ignore` keys.
- Referee: no verification without a verified baseline, repeated calibration rounds, replications by distinct
  agents, replication tasks carry the original command.
- CLI/SDK: no silent `human` agent; `task claim <id>`; `settings.lease_seconds`; experiments link to the
  agent's only leased task; briefs show unjudged findings under the task's tags; worktrees find the main config.
- Core (migration 0007): rerun result claims bind to their evidence, independent reruns determine reproducibility,
  and concurrent claims by one agent respect replication groups. Briefs tolerate legacy partition epsilon values.
- Viewer and MCP: non-loopback HTTP binds require a token, duplicate checks report invalidated seeds,
  and task claims and heartbeats use the current project lease default.
- Hooks: confirm cited record IDs against write receipts and continue replaying after malformed entries or
  concurrent arrivals.
- Autoresearch: keep completed runs across database outages, resolve known legacy partition hashes, recover
  from an invalidated calibration, and decide local KEEP independently of shared replication eligibility.
- Autoresearch benchmark: carry forward each agent's remaining API budget, reconcile collector and ledger
  run receipts, and validate provenance before aggregating repetitions.

## Unreleased: Step 3, autoresearch on many GPUs

- Core: partitions (`settings.partition_key`), per-partition epsilon, verified-best baseline, `update_settings`
  with an audit table, content-addressed blobs, worker intents with exclusive keys, `next_task` exclusions and
  spec matching, views `v_partition_best`, `v_lineage`, `v_inflight`, `v_knob_history` (migration 0005).
- `ml-research` pack: result and calibration claims, config and metrics schemas, its own brief.
- `assay ar`: init, brief, claim, run, replicate, sync, note, idea, status, import, referee; deterministic
  referee (calibration, replication, verdicts); `program.assay.md`; `launch_local.sh`; `assay blob put/get`.
- Viewer: partition selector, verified-best staircase with candidates and refuted runs, in-flight work,
  lineage and knob pages, train.py diffs against the parent.
- CPU simulator with scripted agents and an e2e swarm test; launch experiment harness (`bench/autoresearch`).
- `python -m assay`; `python -m assay.cli` no longer ignores global options in sub-commands.

## 0.1.0.dev0 (unreleased)

- Bound brief and SQL output, keep invalidated experiments out of briefs, and flag claims whose evidence was invalidated.
- Check rerun evidence against the original experiment, serialize decisions, and preserve exact validated configs.
- Preserve distinct hook reports, retry pending captures, and let MCP start before project initialization.
- Isolate real benchmark runs, reconcile evaluator receipts, and measure comparisons under a shared budget.
- Cover long project IDs, escape viewer errors, and enforce install QA on supported Linux and macOS runners.

- M0: embedded Postgres (pixeltable-pgserver, PG 16) with a server that outlives its starter; MCP 2.x and Claude
  Code plugin/hook formats checked; results in DECISIONS.md.
- M1: schema (Alembic revisions running plain SQL), append-only and decision-role triggers, NOTIFY, 10 read views,
  `assay_reader` role; `assay.Ledger` SDK; invariant tests I1-I7 (incl. 32 workers x 500 tasks).
- M2: `assay` CLI with `--json` and exit codes 0/1/2/3.
- M3: deterministic, budgeted brief compiler; property test over 1,000 random ledgers.
- M4: MCP server (stdio + streamable HTTP), worker and orchestrator tool sets, `assay_sql` safety, `assay://schema`.
- M5: Claude Code plugin (MCP, `assay-worker` subagent, `assay` and `assay-lead` skills, result-capture hook), manual
  install files, AGENTS.md snippet.
- M6: read-only viewer (overview with best-so-far curve, experiments, claims board, tasks, record pages; live via SSE).
- M7: circle-packing demo with an LLM-free scripted swarm.
- M8: benchmark harness (A baseline / B files / C assay), `--dry` mode and report generation.
- M9: README (en/ko), concepts, schema, Claude Code, multi-machine and SDK docs.
