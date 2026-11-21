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
4. Records, with no decision: the completion claim in the implementer's name (status `proposed`), the attempt in
   the verifier's name, and a `supports` (every check passed) or `refutes` link from the attempt to the claim.
   An interrupted run is recorded with `run_status = "error"` and no link.
5. Prints the table.

The verifier is `--agent` or `BENCHBOOK_AGENT` when set, else `verifier@<host>` (registered with the harness
role). It must be a different agent from `--reported-by`; the command refuses to verify an agent's own report.

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

## What "verified" will mean

The measurement stage records evidence and decides nothing. When decisions are added, `verified` for a completion
will mean exactly this: **the declared checks passed in an independent re-run** of the reported commit, by an agent
other than the one that reported it, with no guarded path touched and no check changed.

It will not mean the code is correct, that the checks test what the piece promised, or that a reviewer read it.
An exit code only says a command finished; whether the checks test the claim is decided by the person who declares
them. A commit that touched a guarded path or changed `[checks]` will need a person's judgement, however green its
re-run.

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
