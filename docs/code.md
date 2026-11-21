# The code pack

benchbook's `code` pack is for coding work that a lead splits into pieces and hands to implementer agents. The thing
it measures first is the gap between *reported done* and *done*: when an implementer says a piece is complete at a
commit, the lead re-runs the declared checks on that commit in a clean worktree and records the report and the
re-run as a pair. Over many pieces that gives the **false completion rate**: the share of reported completions whose
independent re-run failed.

Nothing changes for the implementers. Their instructions stay as they are; the lead adds one line to its
integration step.

## Set up

```bash
benchbook init --pack code        # same as --pack code --metric checks_passed --direction max
```

The pack's defaults also set `partition_key = "piece"`, so best results and outcomes are computed per piece.

Declare the completion checks and the guarded paths in `.benchbook/config.toml` and commit them to the branch the
pieces start from:

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

A check is a shell command run from the repository root; exit code 0 is a pass. Keys other than `cmd`, `junit`
and `timeout` are errors, so a misspelt key cannot quietly fall back to a default.

Guard globs are matched against the whole repository-relative path: `*`, `?` and `[...]` stay inside one
path segment, a `**` segment spans any number of them (`tests/**` is every file under `tests/`, `**/conftest.py`
is a conftest.py at any depth, the root included). Without `[guard] paths` the three globs above are used.

## The lead's line

In the integration step, for each piece an implementer reported done:

```bash
benchbook check verify <piece> <commit> --reported-by <implementer>
```

Options: `--base REF` (default `[guard] base`, else `origin/main`), `--checks a,b` (a subset of the declared
checks), `--report-file F` (the implementer's report). It prints one row per check and exits 0 only when every
check passed, 1 otherwise; `--json` gives the same as one JSON object.

What it does:

1. Resolves `<commit>` to a full sha.
2. Finds the merge-base of the base and the commit, lists the files changed between them (`git diff --name-only
   --no-renames`, so a deleted or moved-away test shows up under its old path) and keeps those matching the guard
   globs as `guard_files`. `checks_changed` says whether the `[checks]` table of the commit's config.toml differs
   from the merge-base's (parsed, so comments and layout do not count).
3. Runs the checks **declared at the merge-base** in a detached `git worktree` of the commit, each through the core
   runner with its timeout. A commit that rewrites a check's command still gets the merge-base's command, and the
   change shows up as `checks_changed`. The worktree and its git metadata are removed afterwards, also when a check
   times out or the command is interrupted.
4. Records the completion claim in the implementer's name (status `proposed`; under a token, in the verifier's
   name with `payload.reported_by`), the attempt in the verifier's name, and a `supports` (every check passed) or
   `refutes` link from the attempt to the claim. An interrupted run is recorded with `run_status = "error"` and
   no link.
5. Decides, only with lead authority: see [Verified completion](#verified-completion).
6. Prints the table and the decision.

The verifier is `--agent` or `BENCHBOOK_AGENT` when set (with `BENCHBOOK_TOKEN`, the token's agent), else
`verifier@<host>` (registered with the harness role). It must be a different agent from `--reported-by`; the command refuses to verify an agent's own report.

Verifying the same commit again is recorded too (the pack's `dedup_mode = "notify"`): the new attempt names the
first one in `duplicate_of`, and its link goes to the same completion claim.

## What is recorded

One attempt, in the shape of spec §6:

```
config     {"piece": "media-upload-dedupe", "checks": ["pytest-media", "lint"]}
code_ref   "a1b2c3d4..."                       # the full commit sha
metrics    {"checks_passed": 1, "checks_total": 2,
            "checks": {"pytest-media": "fail", "lint": "ok"},
            "failure_signature": "pytest-media:tests.media.test_upload::test_reupload_same_file:AssertionError",
            "guard_touched": false, "guard_files": [], "checks_changed": false}
run_status "ok"                                # the checks ran to the end; a failing check is a result
notes      "check verify: base origin/main 0123..., merge-base 4567...; checks from .benchbook/config.toml ..."
```

A check's status is `ok`, `fail`, `timeout` or `not_run` (after an interruption). The failure signature is
`check:first failing test:exception type` from JUnit, `check:timeout`, or `check:exit N:<hash>` where the hash is
of the last output line with digits zeroed. `guard_files` keeps the first 100 paths; `guard_touched` counts all.

The completion claim: statement `<piece> is complete at <sha12>`, tags `piece:<piece>` and `commit:<sha>`,
payload `{"piece", "commit", "reported_checks"}` and, with `--report-file`, `"report": {"sha256", "bytes"}`.
`reported_checks` lists the declared check names the report mentions (a mention, not a claimed result).

**Never stored:** the diff, the check output, the report's text. Only the commit, the guarded paths it changed, the
failure signature and a hash of the last output line. The output is shown to whoever runs the command and deleted
with the worktree.

## Verified completion

`verified` for a completion means exactly this: **the declared checks passed in an independent re-run** of the
reported commit, by an agent other than the one that implemented it, with no guarded path touched and no check
changed, and no finished attempt of that piece at that commit failed.

It does not mean the code is correct, that the checks test what the piece promised, or that a reviewer read it.
An exit code only says a command finished; whether the checks test the claim is decided by the person who declares
them. A commit that touched a guarded path or changed `[checks]` needs a person's judgement, however green its
re-run.

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
  `min_authors - 1` such agents, each with `checks_passed == checks_total`, `checks_total > 0`, and
  `guard_touched` and `checks_changed` stored as `false`;
- no valid finished attempt of that piece at that commit, cited or not and by anyone, failed a check, touched a
  guarded path or changed `[checks]`. A clean re-run does not wash out another run's guard flag, and runs that
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

The implementer of a completion is the claim's author and the agent named in `payload.reported_by`, if any. The
one exception is a claim recorded for someone else: when the author has the orchestrator, human or harness role
and names a different `reported_by`, only that reporter is the implementer. That is how `check verify` records a
completion under a token (below). A worker that names someone else as the reporter stays an implementer: naming
another reporter can only add to the set. Attempts by the implementer, the reporter's own passing runs included,
never count as independent, and the implementer cannot decide its own completion.

### What `check verify` decides

With lead authority (the verifier has the orchestrator or human role: `--agent lead`, or a lead's token),
`check verify <piece> <commit> --reported-by <implementer>` decides after recording. There are three outcomes:

1. **verified**: every check passed, nothing guarded was touched, `[checks]` is unchanged, and no earlier
   attempt of the piece at the commit disagrees. verify asks the rule first (`Ledger.check_rerun`, which writes
   nothing), then writes `supported` and `verified` (oracle `rerun`). The evidence is the re-run and the
   earlier passing attempts of that commit; `oracle_detail.rerun_check` names the implementer and the
   independent authors.
2. **inconclusive**: an earlier attempt of the piece at the commit disagrees with the re-run. The main case is
   the implementer's own recorded passing attempt against a failing re-run, but any earlier valid finished
   attempt counts, in either direction (a re-run that now passes after an earlier one failed is flaky, and a
   person looks at it). The decision's evidence holds the re-run's id and the disagreeing attempts' ids.
3. **No decision, a person decides**: the commit touched a guarded path or changed `[checks]`. The output names
   the changed files and the `benchbook decide ... --oracle human` line to run.

Otherwise nothing is decided and the output says why: a check failed and no earlier pass disagrees (the refutes
link records it; `refuted` is final, so a person decides it, or the implementer reports a fixed commit), the
claim is already `verified` (a second verify of the same commit is recorded as a repeat with `duplicate_of` and
decides nothing), or the rule refused (its message is printed).

Without lead authority (the default `verifier@<host>` has the harness role) verify records the claim, the attempt
and the link as before, decides nothing, and says so. A lead can decide later from those records.

`--json` adds `decision` (`verified`, `inconclusive` or null), `decision_reason`, `needs_person`, `evidence`
and `decisions`. The exit code still says only whether every check passed.

### Local mode and tokens

In local mode (the owner's database URL), agent names are whatever a process says it is, and roles are given when a
name is first used. The rules above stop an agent from verifying its own report under its own name, and stop the
lead from counting the implementer's own runs. They cannot stop an implementer from reporting as `impl` and
re-running as `impl-2`, or from registering a name with the orchestrator role and deciding. Independence in local
mode catches honest mistakes, not forgery.

With per-agent tokens (a `benchbook_writer` login, [multi-machine.md](multi-machine.md)) every row carries the
agent whose token wrote it. An implementer with a worker token can record attempts and claims only in its own name
and cannot decide anything, so its runs never count for its own completion and the independent re-run is the lead's
(or a CI agent's, with its own token). A token is one agent, so `check verify` under a token cannot propose the
claim in the implementer's name: the verifier proposes it and puts `--reported-by` in `payload.reported_by`,
which makes the reporter the implementer as described above. What tokens do not cover: an operator who issues one
person two names, and the lead's role itself, which is trusted to record for others.

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

- `completions`: reported completions (one claim per piece and commit). `rerun`: those with at least one finished
  re-run. `false_completions`: those with at least one re-run in which a check failed. The rate is over `rerun`;
  invalidated attempts do not count (`v_claim_status` leaves them out).
- `touched_guard` and `changed_checks`: completions whose commit changed a guarded path, or the `[checks]` table.
- The guard columns read the attempts' metrics through `v_leaderboard`, which holds finished runs with a primary
  value, at most 50 per partition. With the pack's defaults (metric `checks_passed`, partition by piece) that is
  every finished verification of a piece up to its 50th. A project created without them gets no rows there.

```bash
benchbook sql "$(cat false-completion.sql)"
```
