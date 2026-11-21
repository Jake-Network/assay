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

The pack's defaults also set `partition_key = "piece"`, so best results and outcomes are computed per piece, and
`worker_intents = true`, so implementers can start their own pieces (path occupancy, below).

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

## Path occupancy

Two pieces that change the same file end in a merge conflict. Before a piece starts, the agent that will work on it
declares the paths it will change; a second piece whose paths overlap a live one's is refused, and the refusal names
the first piece's task id.

```bash
benchbook piece start media-upload --paths 'src/app/modules/media/*' --paths 'tests/media/test_upload.py' \
    [--tags mod:media] [--lease 7200]
benchbook piece list [--tags mod:media] [--all-paths]
benchbook piece done media-upload [--failed] [-m "merged in #42"]
```

Exit codes: 0 done, 1 a user error (a bad glob, no `--paths` on a first start, nothing to release), 2 refused:

```
error: t:3f2a91c0 by impl-a already holds these paths (paths): src/app/modules/media/upload.py and its
src/app/modules/media/* (both match src/app/modules/media/upload.py): piece media-upload
```

**A piece is a task.** `piece start` creates a worker intent (the same mechanism the ml-research pack uses to
occupy knobs): a task tagged `intent`, `piece` and `piece:<name>`, held by the agent that ran the command, with
the spec `{"piece", "paths", "declared"}`. No table, status or queue is added.

**What a piece occupies.** Globs are read as in `[guard]`, from the repository root. Each is expanded against
`git ls-files`: the tracked files it matches go into `paths` (escaped, so `app/[id]/page.tsx` stays that one
file), and a glob that matches no tracked file is kept as the glob, for the files the piece will add. A glob that
matches more than 200 tracked files is also kept as the glob, which occupies those files and any new one under it.
`declared` keeps what was typed. A path typed exactly as a tracked file is that file, brackets included.

**When two pieces overlap.** When some path matches an entry of each: the same file, a file under the other's
glob, or two globs that could both match one path (`src/new/**` and `src/*/x.py` both match `src/new/x.py`).
The glob-against-glob test is exact and errs on the side of refusing: a stored pattern it cannot read counts as
overlapping. A piece also cannot take a piece name another agent holds, whatever the paths. The check runs in the
ledger next to the exclusive-key check, inside the same advisory lock, so of two pieces started at the same moment
exactly one wins.

Expansion happens once, at start. A file added to the branch later is covered only if a glob the piece kept covers
it; a glob that matched files at start is stored as those files, so new files beside them are not occupied. To
reserve a file you will create, name it (or a glob that matches no tracked file yet) in `--paths`.

**How long it lasts.** While the intent's lease is live: 7200 seconds by default (`--lease`).

- *Renew:* run `piece start <piece>` again as the same agent. Without `--paths` it renews the lease and keeps the
  paths; with `--paths` it replaces them (checked against everyone else's, the agent's own other pieces included).
  It is the same task, so the piece keeps its id and its tags (tags given on a restart are not added).
  `benchbook task heartbeat <task>` also renews, but with the project's default lease (900 seconds) unless
  `--lease` is given.
- *Release:* `piece done <piece>` by the holder marks the task done (`--failed`: failed). A lead (orchestrator or
  human) running it on another agent's piece cancels that task, for instance after merging it. `check verify` does
  not release a piece: a refuted completion goes back to its implementer, who still needs the paths.
- *Expire:* when the lease runs out the paths are free; nobody needs to act. A piece started again after that is a
  new task.

**Who holds what.** `piece list` prints one line per live piece (task id, piece, agent, time left, extra tags,
declared globs) and its occupied paths. The same rows are in the reader view `v_inflight` (`spec` holds `piece`,
`paths` and `declared`; `tags` holds the module tags), so a brief can show occupied paths per module:

```
SELECT spec ->> 'piece' AS piece, agent, lease_left_s, spec -> 'paths' AS paths
FROM v_inflight WHERE is_intent AND tags @> ARRAY['mod:media']
```

**Under tokens** (`benchbook_writer` login) the piece is the token's agent's: the intent, its lease and its events
are in that name, and only that agent can renew it or mark it done; a lead's token can cancel it. On an owner
connection the agent is `--agent` / `BENCHBOOK_AGENT`, as for every other write. Workers need
`settings.worker_intents`, which `init --pack code` sets; in an older code project a lead turns it on with
`Ledger.update_settings({"worker_intents": True})`, or starts the pieces itself.

From the SDK the same check is available to any intent: `create_intent(..., exclusive_paths=["paths"])` and
`update_intent(..., exclusive_paths=["paths"])` refuse when the named spec field (a list of globs) overlaps the same
field of a live intent.

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
`check:part` per failing check, where the part names the failing JUnit test, the TypeScript errors, or the exit code
and a hash of the last error line ([Failure signatures](#failure-signatures)). `guard_files` keeps the first 100
paths; `guard_touched` counts all.

The completion claim: statement `<piece> is complete at <sha12>`, tags `piece:<piece>` and `commit:<sha>`,
payload `{"piece", "commit", "reported_checks"}` and, with `--report-file`, `"report": {"sha256", "bytes"}`.
`reported_checks` lists the declared check names the report mentions (a mention, not a claimed result).

**Never stored:** the diff, the check output, the report's text. Only the commit, the guarded paths it changed and
the failure signature (with its hash of the last error line). The output is shown to whoever runs the command and
deleted with the worktree.

A failing verification also prints the earlier attempt of the piece that failed the same way, if there is one, and
leaves a discarded-approach claim ([Discarded approaches](#discarded-approaches)).

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
A run recorded from the implementer's checkout (`metrics.via` set by `check run` or the Bash hook) never counts
as independent either, whoever its author is: the hook records it as `hook`. Its failure still blocks the commit.
A run with `worktree_dirty` true did not test the commit it names and is no attempt of that commit at all.

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

## Failure signatures

A failed check is signed so that the same failure gives the same short string in every run, and a different failure
a different one (`src/benchbook/signature.py`, the one place a signature is made). Per failing check, from the most
specific source the run has:

| source | part | example |
| --- | --- | --- |
| JUnit XML (`junit = true`) | the first failing test id in sorted order and its exception type; with several, how many more and a hash of the whole set | `tests.media.test_upload::test_reupload:AssertionError`, `tests.a::test_x:KeyError +2 more #3fa2b1c0` |
| TypeScript compiler output (`error TS2322` lines, plain or `--pretty`) | the first (code, file) pair in sorted order, the same way | `tsc TS2322 src/media/upload.ts` |
| anything else | the exit code and the sha256 (16 hex) of the normalised last error line | `exit 1:9c1d5e0a7b3f2d14` |
| a timeout | | `timeout` |

The attempt's `failure_signature` joins the failing checks' parts in run order: `unit:exit 1:9c1d...; types:tsc
TS2322 src/a.ts`. Two attempts *fail the same way on a check* when they share one such `check:part` element.

**The last error line** is searched in the last 200 non-empty lines of the output, from the end: the last line with
an error word, an exception or rule name (`ValueError`, `E501`, `TS2304`) or a test runner's failure bullet (`●`,
`✕`); else the last line with a `file:line` location; else the last line. Lines that are the same whichever test
failed are skipped: wrapper lines (`make: ***`, `npm ERR!`, yarn and pnpm's exit lines, cargo's `error: test
failed`, go's `FAIL\tpkg\t0.01s` and `exit status 1`), result counts (`1 failed, 13 passed in 2.31s`, `Found 2
errors in 2 files`, `Tests: 1 failed, 22 total`) and bare section headers (`FAILURES`, `Traceback (most recent call
last):`).

**Normalisation** replaces what changes between runs of one failure: the checkout's own path, timestamps and clock
times, temporary paths (`/tmp/...`, `/var/folders/...`, Windows `...\Temp\...`), uuids, memory addresses, hex ids
of 7 or more characters, durations, and then every free-standing number (line numbers, counts, ports, pids). Numbers
inside a word stay (`E501`, `TS2322`, `x86_64`, `test_case_3`), and the words stay, so two failures with different
messages stay apart. `tests/test_signature.py` pins both directions on realistic pytest, go, Python traceback, jest,
mypy, make/cc and npm outputs.

What it cannot tell apart: two failures whose last error line differs only in numbers (`expected 200, got 503` and
`got 404`), or whose last error line is the same generic message (cargo's ``assertion `left == right` failed`` for
two different tests). Declare `junit = true` with a JUnit reporter (pytest `--junitxml`, jest-junit,
go-junit-report, cargo nextest) where that matters. Without JUnit, pytest's parametrised ids also lose their numbers.

**Where the output is read.** `check run` and `check verify` read the end of the check's log (up to 2 MB) before
deleting it; the Bash hook reads the tool result's error text, which Claude Code may have shortened, so a TypeScript
failure with many errors can sign differently there. A run recorded before this signature existed keeps its old,
rougher signature (digits zeroed in the very last line) and does not match new ones.

**Never stored:** the output, and the line the hash was taken from. Only the signature: test ids and exception types,
TypeScript error codes and file paths, an exit code and a hash.

### Seen before

When a check fails with a signature that an earlier attempt of the same piece already has, the run says so. A match
is a notice; nothing is refused and the exit code is the check's.

```
benchbook: fails fail (exit 3, 0.0s), piece media at 1a2b3c4d5e6f; queued for the ledger (...)
benchbook: seen before: piece media failed the same way in e:91c0a2f1 (impl-a, 2h ago, approach hash-before-put);
3 earlier attempt(s) with this failure, approaches: hash-before-put, retry-loop. A notice: nothing was refused.
```

- *What matches:* valid, finished attempts of the same piece (`config.piece`) whose `failure_signature` shares a
  `check:part` element with this run's, from any session, agent or machine that records into the same ledger:
  `check run`, the Bash hook and `check verify` alike. Runs with uncommitted changes count, marked as such.
  Invalidated attempts do not.
- *`check run`* asks only after a failure, never after a pass, so a passing run costs what it did. The lookup
  opens one plain connection (connect timeout 2 s, statement timeout 1.5 s) in a thread the run waits on for at most
  2 seconds, then prints nothing; any error prints nothing. It runs before the run is queued, so it never finds the
  run itself. It does not start the embedded server: with no `BENCHBOOK_DATABASE_URL` and no running embedded
  server it says nothing. Measured on the test machine (best of 5, a check that prints two lines and exits 3,
  13 ms on its own): a passing `check run` 64 ms over the bare command (no change); a failing one 211 ms over it
  with the ledger up and a match (280 ms with the embedded server running), 187 ms with the database refusing
  connections, and 2.1 s when the database host does not answer at all (the 2 s bound).
- *`check verify`* looks up before it records and prints the same line under the table; `--json` has
  `seen_before` (attempt ids) and `seen_before_approaches`.

## Discarded approaches

A finished attempt of a commit that failed leaves an observation claim meaning "this approach was tried on this piece
and failed this way". The convention (the PR summary counts these):

```
kind       observation
statement  "Approach hash-before-put on piece media-upload failed: unit:tests.media...:AssertionError"
           ("An attempt without a named approach on piece ... failed: ..." when none was named)
tags       discarded
           piece:<piece>
           approach:<name>    when the attempt names its approach
           mod:<name>         each mod: tag of the piece's most recent task (benchbook piece start --tags mod:x)
payload    {"piece", "signature", "approach", "first_attempt", "commit"}
evidence   the attempt, linked supports
author     the attempt's recorder: the verifier, or the agent of check run / the Bash hook (hook by default, the
           token's agent under BENCHBOOK_TOKEN)
```

**One claim per (piece, signature, approach).** The key is `sha256("benchbook:discarded\0" + piece + "\0" +
signature + "\0" + approach)` (approach "" when none), stored as the claim's `statement_hash`, so it does not change
between releases and does not include the author. A later failure with the same key, from anyone, links its attempt
`supports` to the existing claim instead of creating another; `n_support` in `v_claim_status` counts the attempts
(invalidated ones drop out). `payload.first_attempt` and the `mod:` tags are those of the first.

**Which attempts.** Finished attempts (`run_status = "ok"`) with a failing or timed-out check, recorded by `check
run`, the Bash hook or `check verify`. A run with uncommitted changes (`worktree_dirty`) is no attempt of its commit
and leaves no claim (its failure still shows up as "seen before"); an interrupted run has no signature.

**Where the approach comes from.** `config.approach` of the attempt: `benchbook check run <check> --approach NAME`,
or `BENCHBOOK_APPROACH` in the implementer's environment (1-100 characters without spaces; it becomes a tag).
The pack's `dedup_ignore` holds `approach`, so naming an approach never makes a run new. The Bash hook and `check
verify` record no approach.

**When the piece is verified later** the claim stays as it is: a discarded approach records that this approach
failed this way, and a verified commit (likely another approach) does not undo that. A lead who finds the failure was
the environment's, not the approach's, invalidates the attempt (it then stops counting and the claim leaves the brief
when no valid attempt supports it), or refutes the claim.

```bash
benchbook search --all-tags discarded,piece:media-upload      # one piece's discarded approaches
```

## The brief

In a code project `benchbook brief` (MCP `benchbook_brief`) is the code pack's
(`benchbook.packs.code.brief:compile_brief`, `brief_compiler` in pack.toml). Narrowed to a module:

```bash
benchbook brief --tags mod:media                     # comma separated; --budget N (default 2000 tokens)
benchbook brief --tags mod:media,file:src/app/modules/media/
```

It shows, in this order:

1. **Discarded approaches, do not repeat**: the claims above for the pieces in scope, each with its approach, piece,
   signature, first attempt and number of failed attempts; most recently failed first.
2. **Open out-of-scope findings**: [findings](#out-of-scope-findings) still `proposed` or `inconclusive`, matched by
   tag or by `file:` prefix; most reported first.
3. **Occupied paths**: live pieces ([path occupancy](#path-occupancy)) with their holder, time left, tags and paths.
4. **Last verified commit per piece**: the newest `verified` completion of each piece in scope.

**Scope.** A tag matches records that carry it exactly (`mod:media`, `piece:x`). A `file:<prefix>` tag matches by
prefix: the `file:` tags of findings and the paths pieces occupy. A piece is in scope when one of its tasks (`piece
start`, any status) carries a matching tag or occupies a matching path; discarded approaches and verified commits
are found through their piece (and a discarded approach also through its own `mod:` tags). Findings are matched by
their own tags only, as in the [query](#reading-open-findings). A piece never started with `piece start` has no
task, so it is found only without `--tags`. Without `--tags` the brief covers the whole project. With several tags
the sections hold the union.

**Budget.** At most 8 items per section (pack.toml `[brief]`), each line cut to a fixed length; then items are
dropped from the last section upwards until the brief fits `--budget` (with the same 5% slack as the default brief),
and each section says how many it left out (`(+3 more: benchbook piece list)`). Header and footer always stay; a
budget too small for them is an error. Each brief is logged in `brief_log` as before.

`--pack` is not an option: the project's pack already chooses the compiler. Other packs refuse `--tags` rather than
ignore it; their briefs are unchanged.

## Reads that changed a plan

The spec's unit for "a read that changed a plan" is a claim id cited by a plan document. Two things are recorded:
the claims themselves (with their ids, tags and times) and `brief_log` (one row per brief compiled: project, agent,
task, tokens and time; not what it showed). The count, for one plan document:

```bash
ids=$(grep -oE '\bc:[0-9a-f]{8}\b' plan.md | sort -u | sed "s/.*/('&')/" | paste -sd, -)
benchbook sql "WITH cited(ref) AS (VALUES $ids) ..."     # the statement below, with $ids in VALUES
```

```sql
WITH cited(ref) AS (VALUES ('c:1a2b3c4d'), ('c:5e6f7a8b')),
found AS (
  SELECT c.ref, s.id, s.kind, s.tags, s.status, s.created_at
  FROM cited c LEFT JOIN v_claim_status s ON left(s.id::text, 8) = substr(c.ref, 3)
)
SELECT count(DISTINCT ref) AS cited,
       count(DISTINCT id) AS in_ledger,
       count(DISTINCT id) FILTER (WHERE 'discarded' = ANY(tags)) AS discarded_approaches,
       count(DISTINCT id) FILTER (WHERE 'out-of-scope' = ANY(tags)) AS findings,
       count(DISTINCT id) FILTER (WHERE kind = 'completion' AND status = 'verified') AS verified_commits,
       count(DISTINCT id) FILTER (WHERE ('discarded' = ANY(tags) OR 'out-of-scope' = ANY(tags)
                                         OR (kind = 'completion' AND status = 'verified'))
                                        AND created_at < '<plan written>') AS reads_that_changed_the_plan
FROM found
```

`<plan written>` is when the plan was written (its commit time, `git log -1 --format=%cI -- plan.md`). The last
column counts the cited claims of the kinds the brief shows that existed before the plan. Summed over the plans of a
delegate run it is the number the spec's stop rule reads (five delegate runs with none: drop signatures and the
brief, keep verification).

What it can tell: that a plan names a discarded approach, an open finding or a verified commit by its id, and that
the claim was in the ledger before the plan was written. What it cannot: that the id came from a brief rather than
`benchbook search`, `get` or a PR summary (`brief_log` says a brief was compiled, not what it contained, and
`benchbook sql` cannot read it: `Ledger.sql_internal` can); that the citation changed the plan rather than confirmed
it; a read that changed a plan without its id being written down; a plan kept outside files. It counts an upper
bound on citations that came from the brief, and a lower bound on reads that mattered.

## Out-of-scope findings

An implementer often notices problems outside its piece: a bug in a neighbouring module, a flaky test, a missing
index. With one line in `.benchbook/config.toml` the result-capture hook keeps them as claims the lead can decide
on later, instead of leaving them in a report nobody reads again:

```toml
[report]
out_of_scope_heading = "범위 밖"     # or "Out of scope"; the only key of [report]
```

Ask the implementers to end their final report with that section, one top-level list item per finding:

```markdown
## 범위 밖
- `src/app/modules/media/upload.py`: the retry loop ignores the timeout. mod:media
  - reproduced with tests/media/test_upload.py::test_retry
- The auth token refresh races with logout.
```

Nothing else changes for them: no command to run, no id to cite.

### Which reports are read

The same ones the hook already captures (docs/claude-code.md): the final message of a subagent whose type is in
`BENCHBOOK_HOOK_AGENTS`, or the report it handed back with `SubagentHandback` (then the closing text is not read
again). Findings are recorded only in a project created with `--pack code` and only while the heading is set;
anywhere else the hook does exactly what it did before. The heading is read when the hook fires and kept with the
queued report, so `benchbook hook replay` looks for the section the run had. The section is parsed, and the claims
written, by the hook's background processor, where it already writes the auto-captured note; the hook process
itself still only queues the report and exits. A report that cites its own records gets its findings recorded too
(the note is what such a report skips).

### What counts as a finding

- **The section**: a markdown heading of any level, or a line that is bold as a whole (`**범위 밖**`,
  `**Out of scope:**`), whose text equals the configured heading after NFKC normalisation, case folding,
  whitespace collapsing and dropping a trailing colon. A heading section ends at the next heading of the same or a
  higher level (deeper headings inside it are group labels); a bold-line section ends at the next heading of any
  level or the next bold line. A thematic break (`---`) ends either. Every such section in the report is read.
- **The items**: each top-level list item (`-`, `*`, `+`, `1.`, `1)`; a `[ ]` checkbox is dropped) is one finding.
  Its first paragraph, wrapped lines included, is the finding's text. Nested items and indented paragraphs are its
  details. Paragraphs between the items, and lines in fenced code blocks, are never findings. An item whose text is
  empty or only says there is nothing (`none`, `n/a`, `없음`, `해당 없음`, ...) is not a finding. At most 100 per
  report.
- **Paths**: a token of ASCII letters, digits and `_ . - /` counts as a file path when it has a `/` and either sits
  in backticks, ends in `/` or ends in an extension (`src/app/x.py`, `src/app/modules/media/`); without a `/` only
  when it ends in a common source or config extension (`hooks.py`, `config.toml`, but not `os.path` or
  `hooks.process`). A `:12`, `:12:5` or `#L12` suffix and a leading `./` are dropped. An absolute path counts only
  inside the subagent's working directory, and is made relative to it; a path with `..` never counts. Paths come
  from the item's own text, or from its details when the text names none.

### What is recorded

One claim per distinct finding:

```
kind       observation
statement  the item's text (whitespace collapsed; cut at 500 characters)
tags       out-of-scope
           piece:<piece>     when the run recorded attempts (exp run / exp record) of exactly one piece
           mod:<name>        each mod:<name> the item's text writes itself (never inferred from a path)
           file:<path>       each path the finding names, repository-relative, up to 10
payload    {"text", "details", "file", "piece", "heading",
            "source": {"session_id", "event", "agent_id", "agent_type", "tool_name"}}
author     hook (the agent of BENCHBOOK_TOKEN when the hook runs under a token)
```

`payload.source` says which subagent reported the finding, also when the author is a token's agent.
`payload.file` is the first path, the one the dedup key uses. The report itself is stored once as a source
(`uri` `subagent-report:sha256:<sha256 of the report text>`, title naming the subagent type, agent and session) and
linked `supports` to every finding it names. The report text itself stays in the auto-captured note, as before.

**One finding, one claim.** The dedup key is
`sha256("benchbook:out-of-scope\0" + file + "\0" + normalised text)`, where file is the first path ("" for none) and
the text is normalised by dropping backticks and `**`, then as every claim statement is (NFKC, lower case,
whitespace collapsed, punctuation at both ends dropped). It is stored as the claim's `statement_hash`, so it never
changes between releases. It does not include the author: when a second report, from any subagent or token, names
the same finding, its report source is linked `supports` to the existing claim instead of creating another, and
`n_support` in `v_claim_status` counts the reports that named it. The same item twice in one report is one finding,
and a retried report finds its source, claims and links again. So the number of out-of-scope claims equals the
number of distinct items across the reports.

### Deciding

The ledger's existing statuses carry the decision; there is no status of its own for findings. Both decisions are
for an orchestrator or a human (`--as human` in local mode; under a token, the lead's own token and no `--as`), need
an oracle and a rationale of at most 1000 characters, and no evidence (only `verified` requires that).

```bash
# accepted: supported, with the follow-up issue linked as a source
benchbook finding accept c:1a2b3c4d --issue https://github.com/org/repo/issues/12 --oracle human --as human

# rejected: refuted (final in the claim state graph)
benchbook decide c:1a2b3c4d refuted --oracle human --rationale "intended: uploads retry forever by design" --as human
```

`benchbook finding accept` exists because no other command creates a source: it adds the issue URL as a source
(title "follow-up issue"), links it `cites` to the finding, and decides `supported` with that source as the
decision's evidence and the rationale `accepted, follow-up <url>` unless `--rationale` says otherwise. It refuses a
claim without the `out-of-scope` tag and a finding that is already decided, before anything is written. An agent
lead deciding on its own judgement passes `--oracle llm_judge`.

### Reading open findings

A finding is open while nobody has decided it: status `proposed` (or `inconclusive`, which decides nothing).
Accepted and rejected findings drop out. The open findings of one module, by the module's tag or by its path
prefix, in one `benchbook sql` statement:

```sql
SELECT id, statement, tags, n_support AS reports, created_at
FROM v_claim_status
WHERE kind = 'observation' AND 'out-of-scope' = ANY(tags)
  AND status IN ('proposed', 'inconclusive')
  AND ('mod:media' = ANY(tags)
       OR EXISTS (SELECT 1 FROM unnest(tags) AS t WHERE t LIKE 'file:src/app/modules/media/%'))
ORDER BY created_at
```

And the counts a summary needs:

```sql
SELECT count(*) FILTER (WHERE status IN ('proposed', 'inconclusive')) AS open,
       count(*) FILTER (WHERE status = 'supported') AS accepted,
       count(*) FILTER (WHERE status = 'refuted') AS rejected
FROM v_claim_status
WHERE kind = 'observation' AND 'out-of-scope' = ANY(tags)
```
## The implementer's runs: `benchbook check run`

Verification above is the lead's. The implementer's own runs of the declared checks are recorded as well, without
asking it to do anything more than run a check. The lead writes the piece's completion condition as

```bash
benchbook check run <name> [--piece P] [--approach A]
```

- It runs `[checks.<name>]` as the current checkout declares it (the config.toml found from the working directory,
  as every command finds it), from the repository root, through the core runner with tee. The output on stdout is
  the command's, byte for byte (`cmd 2>&1`), and the exit code is the command's: 124 after the check's timeout,
  128+N after signal N. When the check cannot start (no such check, no project, no piece, not in a git repository)
  it exits 2, says why on stderr and records nothing.
- The piece is `--piece`, else `BENCHBOOK_PIECE`, else the current branch name. On a detached HEAD without either it
  does not run.
- What it adds is one line on stderr after the output: `benchbook: unit ok (exit 0, 1.2s), piece media at
  1a2b3c4d5e6f; queued for the ledger (...)`.
- It never waits for the database. The result goes to the local hook spool (one directory per database target, as
  for the result-capture hook) and the spool's background processor records it. A database that is down cannot
  change the exit code or lose the result: the entry waits in the spool and `benchbook hook replay` records it.
  The time it adds to a check is at most 300 ms (`tests/test_check_run.py` measures it against the bare command).

The attempt has check verify's shape, with two more metrics:

```
config     {"piece": "media-upload-dedupe", "checks": ["pytest-media"]}
code_ref   "a1b2c3d4..."                       # HEAD when the check ran
metrics    {"checks_passed": 1, "checks_total": 1, "checks": {"pytest-media": "ok"}, "failure_signature": null,
            "guard_touched": false, "guard_files": [], "checks_changed": false,
            "via": "check-run", "worktree_dirty": false}
notes      "check run: pytest-media in /path/to/checkout; guard against origin/main (merge-base 4567...)"
```

- `via` is `check-run`, or `bash-hook` for a run the Claude Code hook saw (docs/claude-code.md). Either way it is
  the implementer's own run in its own checkout, never an independent re-run, whoever the recorded author is.
- `worktree_dirty` is true when the checkout had uncommitted or untracked paths. `code_ref` is still HEAD (the
  commit the run started from), but the run tested something else: the notes give the number of paths, and the
  completion gate does not count the run. Commit first.
- The guard columns are computed when the processor records the attempt, against the merge-base of the guard base
  and HEAD, as check verify does (uncommitted changes are not in them). When they cannot be computed (no base ref
  locally, no declarations at the merge-base, the checkout is gone) they are left out and the notes say why; a
  missing `guard_touched` is not `false`.
- The author is `--agent` or `BENCHBOOK_AGENT`; without either, `hook` (harness role), or under `BENCHBOOK_TOKEN`
  the token's agent. The processor inherits the environment of the process that started it.
- The failure signature is made as for check verify ([Failure signatures](#failure-signatures)), from the whole
  log. After a failure, a second stderr line names an earlier attempt of the piece that failed the same way
  ([Seen before](#seen-before)); a failed run of a clean checkout leaves a [discarded
  approach](#discarded-approaches). `--approach` (or `BENCHBOOK_APPROACH`) names what the run tries; it is
  recorded as `config.approach`.

## Record coverage

The share of declared check runs that reached the ledger (spec §11, target 95%): ledger attempts over the runs the
Claude Code hook observed.

- **Denominator.** Every Bash call the hook matches to a declared check appends one line to `observed.jsonl` in the
  spool directory of the database target (`~/.local/state/benchbook/spool/<key>/`; `benchbook doctor` prints the
  path on its `hook failures` line). A line has `ts`, `project`, `piece`, `check`, `kind`, `exit_code`,
  `reason`, `session_id`, `tool_use_id` and `harness` (`claude-code`, or `codex`/`cursor`/`gemini` from their
  manifests, docs/harnesses.md; Cursor's lines add `harness_version`). `kind` is `direct` (the hook queued the attempt), `wrapped` (a
  `benchbook check run`, which records itself) or `unrecordable` (piped or chained, in the background,
  interrupted, no exit code, no piece or no commit: observed, not recorded).
- **Numerator.** The attempts with `via` `bash-hook` or `check-run` in the ledger.

```bash
grep -c '"project": "my-project"' "$SPOOL/observed.jsonl"          # observed runs
grep '"project": "my-project"' "$SPOOL/observed.jsonl" | grep -c '"kind": "unrecordable"'
benchbook sql "SELECT metrics ->> 'via' AS via, count(*) AS attempts FROM v_leaderboard WHERE metrics ->> 'via' IN ('bash-hook', 'check-run') AND created_at >= '<since>' GROUP BY 1 ORDER BY 1"
```

Coverage is the sum of `attempts` over the observed count, both from the same start (`<since>`: the first
observation of the delegate run measured). Read it with three limits in mind: `check run` used outside Claude Code
adds attempts no hook observed, so keep the window to the measured run; `v_leaderboard` keeps finished attempts
only (an interrupted run is observed but not counted) and at most 50 per piece; and the observation file is local
to the machine whose hooks wrote it.
