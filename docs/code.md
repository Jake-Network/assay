# The code pack

benchbook's `code` pack is for coding work that a lead splits into pieces and hands to implementer agents. The thing
it measures first is the gap between *reported done* and *done*: when an implementer says a piece is complete at a
commit, the lead re-runs the declared checks on that commit in a clean worktree and records the report and the
re-run as a pair. Over many pieces that gives the **false completion rate**: the share of reported completions whose
independent re-run failed.

Nothing changes for the implementers. Their instructions stay as they are; the lead adds one line to its
integration step.

The code pack's pages: this one (setting up, the lead's line, what verifies a completion, the numbers),
[re-runs and what they record](code-verify.md), [pieces and path occupancy](code-pieces.md),
[the brief and record coverage](code-brief.md), [out-of-scope findings](code-findings.md) and
[CI evidence](code-ci.md).

## Set up

```bash
benchbook init                    # in a git repository, no --metric: the code pack, with the checks it finds proposed
benchbook init --pack code        # same as --pack code --metric checks_passed --direction max
```

The pack's defaults also set `partition_key = "piece"`, so best results and outcomes are computed per piece, and
`worker_intents = true`, so implementers can start their own pieces ([Path occupancy](code-pieces.md#path-occupancy)).

`benchbook init` proposes the checks it finds (`pyproject.toml`, `package.json`, a `Makefile`, `go.mod`,
`Cargo.toml`) and writes them to `.benchbook/config.toml` when you confirm (`--yes` writes them without asking),
with guard paths for the language (Python `tests/**`, `**/conftest.py`, `**/test_*.py`; Node `**/*.test.*`,
`**/*.spec.*`, `**/__tests__/**`; Go `**/*_test.go`; Rust `tests/**`; always `.benchbook/config.toml`) and
`base` from `origin/HEAD`, else a local `main` or `master`. It adds `.benchbook/` to `.git/info/exclude` and pins
the checks in the ledger ([Checks pinned in the ledger](code-verify.md#checks-pinned-in-the-ledger)), so nothing needs committing.
For the team mode (CI as the verifier) commit the config to the branch the pieces start from with
`git add -f .benchbook/config.toml`. The declarations look like this:

```toml
[checks.pytest-media]
cmd = "uv run pytest tests/media --junitxml={junit}"   # {junit} becomes a temporary path outside the worktree
junit = true                                           # failing test ids come from that JUnit XML
timeout = 900                                          # seconds; the check's process group is killed after that

[checks.lint]
cmd = "uv run ruff check src"

[guard]
paths = ["tests/**", "**/conftest.py", ".benchbook/config.toml"]
base = "origin/main"
```

A check is a shell command run from the repository root; exit code 0 is a pass. `timeout` is in seconds (default
900, at most 86400). Keys other than `cmd`, `junit` and `timeout` are errors, so a misspelt key cannot quietly fall
back to a default.

`junit = true` also makes "nothing ran" visible. An exit code alone cannot tell a passing test run from one that
stopped before any test ran (code that exits with status 0 while the tests are collected, a filter that selects
nothing). With `junit = true`, exit code 0 is a pass only when the JUnit file exists and holds at least one testcase
that ran (not skipped) and none that failed or erred; otherwise the check fails with the part `exit 0, no JUnit
report` or `exit 0, no tests ran`, or, when a testcase failed, the failing test's. `check verify` and `check run`
both apply it (`check run` still exits with the command's code and records the failure). A check without `junit` is judged by its exit code only. The JUnit file is written by the code under test's
own process, so this catches a run that ended early, not a commit written to forge the report.

Guard globs are matched against the whole repository-relative path: `*`, `?` and `[...]` stay inside one
path segment, a `**` segment spans any number of them (`tests/**` is every file under `tests/`, `**/conftest.py`
is a conftest.py at any depth, the root included). Without `[guard] paths` the guard is `tests/**`,
`**/conftest.py` and `.benchbook/config.toml`.

## The lead's line

With the Claude Code hook plugin, a coding agent (foreground, or in the background as Claude Code runs every Agent
call by default) can report its commit with a final `DONE <sha>`
line, a `commit: <sha>` or `커밋: <sha>` line, or a single unambiguous full SHA. Benchbook takes the piece
from the commit's `Benchbook-Piece:` trailer, the report's `piece:` line, or a unique local branch pointing to
that commit, in that order. The processor runs the same declared checks in a clean worktree and sends the
result back to the parent session: to the lead, after its next tool result, never to a subagent whose tool call
happens to come first. A completion that needs human review includes that information in the note.

### In a swarm

- Before launching each worker, the lead runs `benchbook piece start <branch> --paths ...` for it. That occupies the
  paths and turns the completion gate on for the branch; a branch with no live piece is not gated
  ([Path occupancy](code-pieces.md#path-occupancy)). Tell each worker its branch, and to end its report with `DONE <full sha>`.
- Each `DONE` queues one re-run. Re-runs run one at a time per database and writer, in their own lane of the hook
  spool, so a worker's `check run` or Bash-hook attempt is recorded within about a second even while re-runs are
  queued. Verdicts arrive one per re-run (`[verify] parallel_baseline` shortens those that need a merge-base
  run, see [The verification worktree](code-verify.md#the-verification-worktree)): merge the first pieces without waiting for every
  verdict.
- Merge a piece after its verdict line (`re-run passed`, `tests not weakened`, `checked: re-run`), not after its
  report. With the Claude Code plugin a lead's `git merge` of a reported commit waits for the verdict and holds a
  merge the verdict does not clear (docs/claude-code.md, "the merge gate").

In the integration step, for each piece an implementer reported done:

```bash
benchbook --agent lead check verify <piece> <commit> --reported-by <implementer>
```

Options: `--base REF` (default `[guard] base`, else `origin/main`), `--checks a,b` (a subset of the declared
checks: the attempt is recorded as partial and never verifies the completion, see below), `--claim ID` (the
completion claim the re-run is evidence for, as `pr pieces --claims` prints it: it must be about the piece and
commit, and its reporter is the implementer, so `--reported-by` may be left out), `--report-file F` (the
implementer's report), `--trace-ref JSON` (a JSON object, or `@file`, stored as the attempt's `trace_ref`: the CI
action puts its run's URL there), `--no-decide` (record only, even with lead authority). It prints one row per
check and exits 0 only when every check passed, 1 otherwise; `--json` gives the same as one JSON object.

What it does:

1. Resolves `<commit>` to a full sha and finds the merge-base of the base and the commit. The files changed between
   them (`git diff --name-only --no-renames`, so a deleted or moved-away test shows under its old path) that match
   the guard globs are `guard_files`. `checks_changed` says whether the `[checks]` table of the commit's
   config.toml differs from the merge-base's (parsed, so comments and layout do not count).
2. Runs the checks **declared at the merge-base** in a detached `git worktree` of the commit, each through the core
   runner with its timeout, so a commit that rewrites a check's command still gets the merge-base's. The worktree is
   removed afterwards, also after a timeout or an interrupt. The checks run without the ledger's credentials
   (`BENCHBOOK_TOKEN`, `BENCHBOOK_DATABASE_URL`, `BENCHBOOK_READER_PASSWORD`, their `ASSAY_` forms, `PGPASSWORD`,
   `PGPASSFILE`): the code under test is the implementer's and must not write as the verifier. The rest of the
   verifier's environment is kept; a check that needs a database password takes it from a variable of another name.
   `check run` runs in the implementer's own environment.
3. Records the completion claim of the report (status `proposed`; in the implementer's name, or under a token in
   the verifier's name with `payload.reported_by`; see [the completion claim](code-verify.md#what-is-recorded)), the attempt in
   the verifier's name, and a `supports` (every check passed) or `refutes` link from the attempt to the claim. An
   interrupted run is recorded with `run_status = "error"` and no link.
4. Decides, only with lead authority ([Verified completion](#verified-completion)), and prints the table and the
   decision.

The verifier is `--agent` or `BENCHBOOK_AGENT` when set (with `BENCHBOOK_TOKEN`, the token's agent), else
`verifier@<host>` (registered with the harness role), so a re-run without a name is recorded and not decided. It
must be a different agent from `--reported-by`; the command refuses to verify an agent's own report. The hook's
automatic re-run of a reported `DONE <sha>` runs as `rerun@<host>` (registered with the orchestrator role), or as
the token's agent under `BENCHBOOK_TOKEN`.

Verifying the same commit again is recorded too (the pack's `dedup_mode = "notify"`): the new attempt names the
first one in `duplicate_of`, and its link goes to the same completion claim.

## Verified completion

`verified` for a completion means exactly this: **the declared checks passed in an independent re-run** of the
reported commit, by an agent other than the one that implemented it, with no test weakened and no check changed,
and no finished attempt of that piece at that commit failed.

It does not mean the code is correct, that the checks test what the piece promised, or that a reviewer read it.
An exit code only says a command finished; whether the checks test the claim is decided by the person who declares
them. A commit that weakened a test ([Added tests and weakened tests](code-verify.md#added-tests-and-weakened-tests)) or changed
`[checks]` needs a person's judgement, however green its re-run; a commit that only adds tests does not.

### The policy

The code pack's `pack.toml` declares it:

```toml
[verify]
rule = "all_pass"
min_runs = 1      # passing independent re-runs needed
min_authors = 2   # distinct agents needed, counting the implementer: the implementer and one other
```

`benchbook decide <claim> verified --oracle rerun --evidence <attempts>` on a completion is accepted only when all of
these hold; otherwise it is refused, and the message says why and that a person decides with `--oracle human`:

- every evidence id is a valid (not invalidated) attempt with `run_status = "ok"` of the claim's piece
  (`config.piece`) at its commit (`code_ref`);
- at least `min_runs` of them are by agents other than the implementer (see below), from at least
  `min_authors - 1` such agents, each with every name in `checks_declared` among its `checks` (not partial),
  `checks_passed == checks_total`, `checks_total > 0`, and one `ok` result per counted check. Both
  `guard_weakened` and `checks_changed` must be stored as `false` (an attempt without `guard_weakened` is read by
  `guard_touched`);
- no valid finished attempt of that piece at that commit, cited or not and by anyone, failed a check, weakened a
  test or changed `[checks]`. A clean re-run does not wash out another run's guard flag, and runs that
  disagree are a person's call. A lead who finds a run was broken (no network, wrong machine) invalidates it
  (`benchbook decide <attempt> invalidated --oracle human ...`) and it stops counting;
- the deciding agent is not the implementer, whatever the oracle (`human` included).

The `human` oracle stays open for everything else: a person can verify a commit that changed a test on purpose,
and the decision is recorded with oracle `human`. Other packs (core, ml-research) keep the replication rule
(`rule = "replication"`, the default): seeds, authors and the baseline, as in [concepts.md](concepts.md).

Claim status is the latest decision (`v_claim_status`); refuting links are counted there (`n_refute`) but do not
change the status by themselves. What keeps a completion with a failing re-run from being verified is the third
rule above, not the view. The state graph still applies: a claim goes `proposed -> supported -> verified`, and
`refuted` is final.

### Who the implementer is

The *reporter* of a completion claim is the agent in `payload.reported_by` when the claim's author has the
orchestrator, human or harness role (it records a report for someone else, as `check verify` does under a token,
below), and otherwise the author itself, whatever name a worker put in its payload.

The implementers of a completion claim are:

- the claim's author and the agent named in `payload.reported_by`, if any; when the author has the orchestrator,
  human or harness role and names a different `reported_by`, only that reporter. A worker that names someone else
  stays an implementer: naming another reporter can only add to the set;
- and the reporter of every other completion claim of the same piece and commit (same payload `piece` and
  `commit`, or both tags), whoever wrote it. A second claim can add a name and never take one out, so an agent
  cannot drop itself from the set by having a lead or CI record the piece again. An agent that claims someone else's
  piece only makes itself an implementer of it too. A worker's `reported_by` reaches only its own claim, so a worker
  cannot put the lead or CI into the set of someone else's completion.

Attempts by an implementer, the reporter's own passing runs included, never count as independent, and an
implementer cannot decide the completion. The rule (`decide`, `check_rerun`), `check verify`'s decision and
`pr summary` read the same set.
A run recorded from the implementer's checkout (`metrics.via` set by `check run` or the Bash hook) never counts
as independent either, whoever its author is: the hook records it as `hook`. Its failure still blocks the commit.
A run with `worktree_dirty` true did not test the commit it names and is no attempt of that commit at all.

### What `check verify` decides

With lead authority (the verifier has the orchestrator or human role: `--agent lead`, or a lead's token),
`check verify <piece> <commit> --reported-by <implementer>` decides after recording. There are three outcomes:

1. **verified**: the rule above accepts the re-run. verify asks it first (`Ledger.check_rerun`, which writes
   nothing), then writes `supported` and `verified` (oracle `rerun`). The evidence is the re-run and the earlier
   passing attempts of that commit; `oracle_detail.rerun_check` names the implementer and the independent authors.
2. **inconclusive**: an earlier valid finished attempt of the piece at the commit disagrees with the re-run, in
   either direction (the implementer's passing run against a failing re-run, or a re-run that now passes after one
   failed). The decision's evidence holds the re-run's id and the disagreeing attempts' ids.
3. **No decision, a person decides**: the commit weakened a test or changed `[checks]`. The output names
   the changed files and the `benchbook decide ... --oracle human` line to run.

Otherwise nothing is decided and the output says why: a check failed and no earlier pass disagrees (the refutes
link records it; `refuted` is final, so a person decides it, or the implementer reports a fixed commit), the
claim is already `verified` (a second verify of the same commit is recorded as a repeat with `duplicate_of`), or
the rule refused (its message is printed). Without lead authority (the default `verifier@<host>` has the harness
role) verify records the claim, the attempt and the link, decides nothing, and says so; a lead can decide later
from those records.

The automatic re-run of a subagent's report (`benchbook hook agent-result`) decides as `rerun@<host>` (orchestrator
role), or as the token's agent with the token's role. When it passed and still decided nothing, its line says why
after `checked: none`, for example `checked: none (recorded by a worker agent; a lead decides)`.

It also retries a failure once. When every declared check ran, one failed, nothing guarded was weakened and
`[checks]` did not change, the failed run is recorded first (`metrics.retry`: `failed; retried`), with no discarded
approach and under an id derived from the request's receipt, so a request interrupted during the retry only
retries the next time. Then every declared check runs again in a fresh worktree (with the JUnit count comparison
when guarded files changed), recorded under the receipt with `metrics.retry_of` (the failed run) and
`metrics.first_signature`:

| the retry | `metrics.retry` | line | decision, dead end |
|---|---|---|---|
| passes | `passed on a retry after a failed run` | `re-run failed (<signature>), then passed on a retry ... attempts disagree (c:…; failed run e:…)` | **inconclusive** (the runs disagree); none |
| fails the same way | `failed again on a retry` | `re-run failed, and again on a retry ... (c:…; runs e:…, e:…)` | none decided; the retry leaves the discarded approach |
| fails differently | `failed again on a retry, differently` | `... and again on a retry with a different failure ...` | none decided; no dead end, since neither failure repeated |
| cannot run (its prepare step failed) | `retry could not run` | `re-run failed` | the original failure is the only check evidence and leaves the dead end. Processing the same receipt again returns that result |

A passing retry that ran fewer tests requires a person's review. What the lead can run after each verdict is in
docs/claude-code.md, "the merge gate". `check verify` run by hand does not retry.

A lead who finds a failure is not the piece's (a flaky test, the machine) invalidates the failed run and runs
`check verify` itself, with `--reported-by`, which then decides from the passing runs:

```bash
benchbook --agent lead decide e:<failed run> invalidated --oracle human --rationale "flaky socket test, not the piece"
benchbook --agent lead check verify <piece> <sha> --reported-by <worker>
```

`--json` adds `decision` (`verified`, `inconclusive` or null), `decision_reason`, `needs_person`, `evidence`
and `decisions`. The exit code still says only whether every check passed.

### Local mode and tokens

In local mode (the owner's database URL), agent names are whatever a process says it is, and roles are given when a
name is first used. The rules above stop an agent from verifying its own report under its own name, and stop the
lead from counting the implementer's own runs. They cannot stop an implementer from reporting as `impl` and
re-running as `impl-2`, or from registering a name with the orchestrator role and deciding. Independence in local
mode catches honest mistakes, not forgery.

With per-agent tokens ([multi-machine.md](multi-machine.md)) every row carries the agent whose token wrote it. A
worker token records only in its own name and decides nothing, so its runs never count for its own completion and
the independent re-run is the lead's or a CI agent's. Since a token is one agent, `check verify` under a token
proposes the claim in the verifier's name with `--reported-by` in `payload.reported_by`, which makes the reporter
the implementer ([Who the implementer is](#who-the-implementer-is)). Tokens do not cover an operator who issues one
person two names, nor the lead's role itself, which is trusted to record for others.

## The numbers

The false completion rate and the number of completions that touched guarded paths, in one `benchbook sql`
statement over the reader's views:

```sql
WITH done AS (
  SELECT id, tags, n_support, n_refute FROM v_claim_status WHERE kind = 'completion'
), touched AS (
  SELECT d.id,
         bool_or((e.metrics ->> 'guard_touched')::boolean) AS guard_touched,
         bool_or((e.metrics ->> 'checks_changed')::boolean) AS checks_changed
  FROM done d
  JOIN v_leaderboard e ON d.tags @> ARRAY['piece:' || (e.config ->> 'piece'), 'commit:' || e.code_ref]
  GROUP BY d.id
)
SELECT count(*) AS completions,
       count(*) FILTER (WHERE n_support + n_refute > 0) AS rerun,
       count(*) FILTER (WHERE n_refute > 0) AS false_completions,
       round(count(*) FILTER (WHERE n_refute > 0)::numeric
             / nullif(count(*) FILTER (WHERE n_support + n_refute > 0), 0), 3) AS false_completion_rate,
       count(*) FILTER (WHERE t.guard_touched) AS touched_guard,
       count(*) FILTER (WHERE t.checks_changed) AS changed_checks
FROM done d LEFT JOIN touched t USING (id)
```

-  `completions`: reported completions (one claim per piece, commit and reporter). `rerun`: those with at least one
  finished re-run linked to them: a pass of every declared check, or a failure (a pass of only some checks is not
  linked). `false_completions`: those with at least one re-run in which a check failed. The rate is over `rerun`;
  invalidated attempts do not count (`v_claim_status` leaves them out).
- `touched_guard` and `changed_checks`: completions whose commit changed a guarded path, or the `[checks]` table.
- The guard columns read the attempts' metrics through `v_leaderboard`, which holds finished runs with a primary
  value, at most 50 per partition. With the pack's defaults (metric `checks_passed`, partition by piece) that is
  every finished verification of a piece up to its 50th. A project created without them gets no rows there.

```bash
benchbook sql "$(cat false-completion.sql)"
```
