# Python SDK

```python
from benchbook import Ledger

Ledger.init_project("lr-sweep", goal="Minimise val loss", metric="val_loss", direction="min")  # pack="core" by default
lg = Ledger.open(project="lr-sweep", agent="worker-1")     # or BENCHBOOK_PROJECT / .benchbook/config.toml, BENCHBOOK_AGENT
lg = Ledger.open(project="lr-sweep", token="bbt_...")      # or BENCHBOOK_TOKEN: the token names the agent (docs/multi-machine.md)
```

`init_project` defaults to the `core` pack; the CLI's `benchbook init` without `--metric` makes a `code` project.
The code-mode commands (`check verify`, pieces, findings) are in [code.md](code.md).

Workers:

| call | returns |
|---|---|
| `next_task(tags=None, lease_seconds=None, spec_match=None, task_id=None)` | `Task` or `None`; the lease defaults to `settings.lease_seconds` (900); skips tasks whose `spec.exclude_agents` lists you, and a task with `spec.distinct_agents_for = "<key>"` when you already claimed one with the same `spec[<key>]`; `spec_match` keeps tasks whose spec contains that JSON; `task_id` claims that one task if it is claimable |
| `claim_task(task_id, lease_seconds=None)` | `Task`; raises `Conflict` when that task is not claimable by you |
| `heartbeat(task_id, lease_seconds=None)` | new expiry (default lease as above); raises `LeaseLost` (also once the lease has run out) |
| `brief(task_id=None, budget_tokens=2000, partition=None, tags=None, repo=None, max_chars=None)` | markdown; `tags`, `repo` and `max_chars` are for the code pack's brief ([code.md](code.md#the-brief)) |
| `check_tried(config, code_ref=None)` | `TriedResult(exact, replicates, best_value, dedup_key)` |
| `record_experiment(config, metrics, run_status="ok", seed=None, code_ref=None, notes=None, artifacts=None, task_id=None, trace_ref=None, duration_ms=None, tokens=None, cost_usd=None)` | `RecordResult(id, outcome, is_new_best, duplicate_of, notice)`; an exact duplicate has `id=None`, or a new `id` plus `duplicate_of` and `notice` in a `dedup_mode = "notify"` pack ([concepts.md](concepts.md#duplicates-and-replicates)) |
| `propose_claim(kind, statement, payload=None, tags=None, task_id=None, evidence=[(id, "supports")])` | `ClaimResult(id, duplicate_of)` |
| `link(src, relation, dst, note=None)` | link id |
| `add_source(uri, title=None, locator=None, sha256=None)` | source id |
| `complete_task(task_id, status="done", summary="")` | `Task`; raises `LeaseLost` |
| `create_intent(title, spec=None, tags=None, lease_seconds=900, exclusive=(), exclusive_paths=())` | a task already claimed by you, tagged `intent` (needs `settings.worker_intents` for workers; under a token the database enforces it too, and caps every lease at `settings.max_lease_seconds`, default 86400); `Conflict` when a live intent has the same value for an `exclusive` spec field, or when an `exclusive_paths` spec field (a list of path globs) overlaps the same field of a live intent |
| `update_intent(task_id, spec_patch, lease_seconds=None, exclusive=(), exclusive_paths=())`, `my_intents()` | your live intents |
| `put_blob(data, media_type)`, `get_blob(uri)` | `benchbook://blob/<sha256>`; bytes |
| `baseline(partition=None)` | `(value, epsilon)` that the next outcome is compared with |
| `check_rerun(claim, evidence_ids)`, `completion_attempts(claim)` | what `decide(claim, "verified", "rerun", ...)` would check, without writing (raises `InvalidTransition`); a completion's piece, commit, implementers, status and attempts ([code.md](code.md#verified-completion)) |
| `get(id)`, `search(text, kind, status, tags, limit, all_tags)` (`tags`: any of them, `all_tags`: every one), `status()`, `export()` | reads |

Orchestrator / human only (`PermissionDenied` otherwise): `create_tasks([...])`, `cancel_task(id, reason)`,
`update_settings(patch)` (JSON merge patch, audited in `settings_events`),
`decide(target, new_status, oracle, rationale, evidence_ids, oracle_detail=None, force=False)`.
`force` allows a transition outside the claim graph. The oracle and evidence requirements still apply.
`sql(query)` (read-only, `v_*` views) is open to every role in the SDK; the MCP server offers it in the orchestrator
tool set only.

A brief may shorten the displayed goal and task details to stay within the requested budget. A budget too small
for the required brief structure raises `ValidationFailed`.

Ids: every call accepts a short id (`e:91c0a2f1`) or a full UUID. Errors: `ValidationFailed` (with `.problems`, one
line per bad field), `NotFound`, `Ambiguous`, `PermissionDenied` (and its subclass `InvalidAgentToken`: the database
rejected the token for this project), `LeaseLost`, `InvalidTransition`, `Conflict`, all subclasses of
`BenchbookError`.

A full loop is in `examples/sdk-loop/loop.py`.

## Running a command

`benchbook.core.runner` is the runner behind `benchbook ar run`, with nothing of autoresearch in it. It does not touch
the ledger: it runs one shell command and says how that went.

```python
from benchbook.core.runner import Interrupted, run_command

res = run_command("uv run pytest tests/media", cwd, log_path, timeout=900, tee=True)
res.run_status, res.exit_code, res.duration_ms, res.log_tail, res.reason, res.metrics
```

`run_command(cmd, cwd, log_path, timeout, metric=None, parse_regex=..., env=None, tee=False)`:

- The command runs through the shell in its own process group, with stdin closed and stdout and stderr together in
  `log_path` (overwritten). `env=None` inherits the environment.
- `run_status` is `ok` for exit code 0, `error` for any other exit code (`reason` `exit N`), `timeout` when
  `timeout` seconds passed: then the whole group is killed and `exit_code` is `None`.
- `metric="name"` also asks for a `name: number` line in the output (`error`, `no name in the summary`, when it is
  missing). `metrics` holds every such line either way. `benchbook.integrations.autoresearch.runner.run_command` is
  the same function with `metric="val_bpb"` as its default.
- `tee=True` also writes the output to this process's stdout while the command runs: the same bytes in the same
  order as `cmd 2>&1`. The command sees a pipe, not a terminal, so stderr arrives on stdout. Output from a
  background process that the command left running is copied for 2 more seconds, then dropped.
- `duration_ms` is wall clock. `log_tail` is the end of the output: its last 30 lines, cut to their last 1500
  characters. The whole output is in `log_path`.
- Ctrl-C raises `KeyboardInterrupt`, SIGTERM and SIGHUP raise `Interrupted` (when called from the main thread),
  both after the group was killed. Catch them to record the run from `log_path`, then re-raise.

## Check, run and record in one command

`benchbook exp run` puts the ledger around this runner. An agent with a shell types one command per attempt instead
of `exp tried`, the evaluation and `exp record` (three tool calls, each a turn that re-reads the context):

```bash
benchbook exp run --config '{"lr": 3e-4}' --seed 0 --code-ref v3 -- python eval.py
```

- **Before running** it calls `check_tried` (which also validates the config, so a bad one never runs). When this
  config, code_ref and seed already ran, a pack with `dedup_mode = "refuse"` stops there: it prints the earlier run
  (id, author, outcome, value) and exits 2, the code `exp record` uses for a refused duplicate. An earlier ok run,
  invalidated or not, always stops it, because the ledger would refuse the record. A run that only failed is run
  again with `--retry-failed`, which is what the ledger allows. A `"notify"` pack runs it and reports the earlier
  id (stderr before the run, `duplicate_of` and `notice` after). Other seeds of the same config are replicates:
  they run, with a note on stderr naming the earlier runs.
- **The command** is everything after `--`. One argument is a shell command line as written; several are quoted and
  joined. It runs through `run_command(..., tee=True)` in the current directory with `--timeout` (default 600 s),
  so its output appears as it runs, unchanged. `BENCHBOOK_EXP_CONFIG` (the config as JSON) and
  `BENCHBOOK_EXP_SEED` are set in its environment, so it can read the config instead of repeating it.
- **Metrics** come from the output in the formats the examples already print: `key: number` lines
  (`parse_summary`) and a JSON object on the last line, as `examples/circle-packing/evaluator.py` prints it. Its
  numbers are metrics (or those of a `"metrics"` object), `"run_status"` and `"invalid"` (a reason) can mark the
  attempt invalid or failed, `"notes"` goes to notes, and `"value"` stands for the primary metric when the object
  does not name it (the receipt of `bench/tools/evaluate.py`).
- **Every run that starts is recorded**: exit code 0 is `ok` (unless the output says otherwise, or a metric the pack
  requires with `ok_metrics_required` is missing: `error`), any other exit code `error`, the timeout `timeout`, and
  Ctrl-C, SIGTERM or SIGHUP `error` with `interrupted (SIGTERM)` in notes. A failure's notes end with the tail of
  its output. `trace_ref` holds `cmd` and `exit_code` (merged with `--trace-ref`), `duration_ms` the wall clock.
  The other options are those of `exp record`: `--code-ref`, `--notes`, `--task` (default: the one task you hold a
  lease on), `--artifacts`, `--tokens`, `--cost-usd`; the agent is `--agent` or `BENCHBOOK_AGENT`.
- **Output**: after the command's own output, one line on stdout that starts with `benchbook: `, for example
  `benchbook: recorded e:1a2b3c4d improved sum_radii=2.63  ★ new best (ok, 1.2s)`, or `benchbook: not run:
  duplicate of e:...`. With `--json` that last line is one JSON object (`ok`, `ran`, `id`, `outcome`,
  `run_status`, `exit_code`, `metrics`, `duplicate_of`, ...). Notes go to stderr. The capture hook reads this
  line as a write receipt, like the output of `exp record`.
- **Exit code**: the command's own; 124 after the timeout; 128+N after signal N (130 for Ctrl-C), after the run is
  recorded; 2 when a duplicate was not run, or was run but not recorded because another agent recorded the same
  run meanwhile; 1 for usage errors. When the run cannot be recorded (the database is unreachable) it exits 1 and
  keeps the output log, whose path the error names. Unlike `benchbook ar run` there is no local queue.
