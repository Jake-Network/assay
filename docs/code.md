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
benchbook init                    # in a git repository, no --metric: the code pack, with the checks it finds proposed
benchbook init --pack code        # same as --pack code --metric checks_passed --direction max
```

The pack's defaults also set `partition_key = "piece"`, so best results and outcomes are computed per piece, and
`worker_intents = true`, so implementers can start their own pieces ([Path occupancy](#path-occupancy)).

`benchbook init` proposes the checks it finds (`pyproject.toml`, `package.json`, a `Makefile`, `go.mod`,
`Cargo.toml`) and writes them to `.benchbook/config.toml` when you confirm (`--yes` writes them without asking),
with guard paths for the language (Python `tests/**`, `**/conftest.py`, `**/test_*.py`; Node `**/*.test.*`,
`**/*.spec.*`, `**/__tests__/**`; Go `**/*_test.go`; Rust `tests/**`; always `.benchbook/config.toml`) and
`base` from `origin/HEAD`, else a local `main` or `master`. It adds `.benchbook/` to `.git/info/exclude` and pins
the checks in the ledger ([Checks pinned in the ledger](#checks-pinned-in-the-ledger)), so nothing needs committing.
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
  ([Path occupancy](#path-occupancy)). Tell each worker its branch, and to end its report with `DONE <full sha>`.
- Each `DONE` queues one re-run. Re-runs run one at a time per database and writer, in their own lane of the hook
  spool, so a worker's `check run` or Bash-hook attempt is recorded within about a second even while re-runs are
  queued. Verdicts arrive one per re-run (`[verify] parallel_baseline` shortens those that need a merge-base
  run, see [The verification worktree](#the-verification-worktree)): merge the first pieces without waiting for every
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
   the verifier's name with `payload.reported_by`; see [the completion claim](#what-is-recorded)), the attempt in
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

**A piece is a task.** `piece start` creates a worker intent (the mechanism the ml-research pack uses to occupy
knobs): a task tagged `intent`, `piece` and `piece:<name>`, held by the agent that ran the command, with the spec
`{"piece", "paths", "declared"}`. While it is live, its branch is gated by the Claude Code completion gate
(docs/claude-code.md, `benchbook hook task-completed`): a task completed there needs a passing run of every
declared check at HEAD. A branch with no live piece is not gated, so a lead on its own feature branch completes its
to-do items freely.

**What a piece occupies.** Globs are read as in `[guard]`, from the repository root, and expanded once, at start,
against `git ls-files`: the tracked files a glob matches go into `paths` (escaped, so `app/[id]/page.tsx` stays that one
file). A glob that matches no tracked file, or more than 200, is kept as the glob, which occupies any file it matches,
new ones included. `declared` keeps what was typed; a path typed exactly as a tracked file is that file, brackets
included. A file added later is covered only if a kept glob covers it, so to reserve a file you will create, name it (or
a glob that matches no tracked file yet) in `--paths`.

**When two pieces overlap.** When some path matches an entry of each: the same file, a file under the other's
glob, or two globs that could both match one path (`src/new/**` and `src/*/x.py` both match `src/new/x.py`).
The glob-against-glob test is exact and errs on the side of refusing: a stored pattern it cannot read counts as
overlapping. A piece also cannot take a piece name another agent holds. The check runs in the ledger inside the
same advisory lock as the exclusive-key check, so of two pieces started at the same moment exactly one wins.

**How long it lasts.** While the intent's lease is live: 7200 seconds by default (`--lease`).

- *Renew:* run `piece start <piece>` again as the same agent. Without `--paths` it renews the lease and keeps the
  paths; with `--paths` it replaces them (checked against everyone else's, the agent's own other pieces included).
  It is the same task, so the piece keeps its id and its tags (tags given on a restart are not added).
  `benchbook task heartbeat <task>` also renews, with the project's default lease (900 seconds) unless `--lease`
  is given. Both renew only a live lease, and every lease counts from the database's clock after the overlap check's
  lock. Once a lease has run out another piece may hold the paths: a heartbeat fails with "lease lost", and
  `piece start <piece>` starts a new piece through the overlap check, taking the paths the expired piece declared
  unless `--paths` is given. A piece released with `piece done` needs `--paths` again. Renewing, restarting and
  releasing each read the current generation in the same transaction, under the project intent lock.
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

**Under tokens** (`benchbook_writer` login) the piece is the token's agent's: only that agent can renew it or mark
it done, and a lead's token can cancel it. On an owner connection the agent is `--agent` / `BENCHBOOK_AGENT`.
Workers need `settings.worker_intents` (JSON `true`), which `init --pack code` sets; in a project without it a lead
sets it with `Ledger.update_settings({"worker_intents": True})`, or starts the pieces itself. The database enforces
that setting and the lease cap ([multi-machine.md](multi-machine.md#what-it-prevents-and-what-it-does-not)).

**A limit under tokens:** the overlap check (piece name and paths) runs in the SDK, not in the database. A worker
that writes the task row with its own SQL on the writer login can still take a piece name or paths another agent
holds, for at most the lease cap; the row is in its name, and `piece list` and `v_inflight` show it. Path occupancy
prevents accidental collisions between agents that use benchbook, not a writer set on causing one.

From the SDK the same check is available to any intent: `create_intent(..., exclusive_paths=["paths"])` and
`update_intent(..., exclusive_paths=["paths"])` refuse when the named spec field (a list of globs) overlaps the same
field of a live intent.

## What is recorded

One attempt:

```
config     {"piece": "media-upload-dedupe", "checks": ["pytest-media", "lint"]}
code_ref   "a1b2c3d4..."                       # the full commit sha
metrics    {"checks_passed": 1, "checks_total": 2,
            "checks": {"pytest-media": "fail", "lint": "ok"}, "checks_declared": ["pytest-media", "lint"],
            "failure_signature": "pytest-media:tests.media.test_upload::test_reupload_same_file:AssertionError",
            "guard_touched": false, "guard_files": [], "checks_changed": false}
run_status "ok"                                # the checks ran to the end; a failing check is a result
notes      "check verify: base origin/main 0123..., merge-base 4567...; checks from .benchbook/config.toml ..."
```

A check's status is `ok`, `fail`, `timeout` or `not_run` (after an interruption). The failure signature is
`check:part` per failing check, where the part names the failing JUnit test, the TypeScript errors, or the exit code
and a hash of the last error line ([Failure signatures](#failure-signatures)). `guard_files` keeps the first 100
paths; `guard_touched` counts all. `checks_declared` names every check declared at the merge-base, run or not: a run
of fewer (`--checks`) is *partial*. It is recorded and its failures count (a failing partial run refutes the claim),
but a partial pass is never evidence of `verified`: it gets no `supports` link, so it is not a re-run in
[the numbers](#the-numbers) either, and `check verify` decides nothing on it.

The completion claim: statement `<piece> is complete at <sha12>`, tags `piece:<piece>` and `commit:<sha>`,
payload `{"piece", "commit", "reported_checks"}` and, with `--report-file`, `"report": {"sha256", "bytes"}`.
`reported_checks` lists the declared check names the report mentions (a mention, not a claimed result).

A claim's identity is the triple (piece, commit, reporter), not its statement. `check verify` reuses a claim only
when it carries both tags, its payload names the same piece and commit, and it records this reporter's report,
whatever its statement: its reporter is `--reported-by` (see [Who the implementer is](#who-the-implementer-is))
or the verifier wrote it exactly as this call would. Otherwise it proposes a new one. When another agent's claim
already holds the usual statement, the new claim's statement names the reporter:
`<piece> is complete at <sha12>, reported by <reporter>`; when that one is taken too, it keeps that text under a
random `statement_hash`. So nobody can take a completion first: a claim with the usual statement and the right
payload does not give its author the credit, and one with a wrong commit in its payload does not block the piece.

`statement_hash` is computed by the client (`benchbook.core.dedup.statement_hash`, or a caller's own hash for
discarded approaches and findings); the database checks only that it is unique in the project, so nothing reads a
claim's identity from it.

**Never stored:** the diff, the check output, the report's text. Only the commit, the guarded paths it changed and
the failure signature (with its hash of the last error line). The output is shown to whoever runs the command and
deleted with the worktree.

A failing verification also prints the earlier attempt of the piece that failed the same way, if there is one, and
leaves a discarded-approach claim ([Discarded approaches](#discarded-approaches)).

## Verified completion

`verified` for a completion means exactly this: **the declared checks passed in an independent re-run** of the
reported commit, by an agent other than the one that implemented it, with no test weakened and no check changed,
and no finished attempt of that piece at that commit failed.

It does not mean the code is correct, that the checks test what the piece promised, or that a reviewer read it.
An exit code only says a command finished; whether the checks test the claim is decided by the person who declares
them. A commit that weakened a test ([Added tests and weakened tests](#added-tests-and-weakened-tests)) or changed
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

## Checks pinned in the ledger

`check verify` reads the `[checks]` committed at the merge-base when there are any: that is the team mode, and
nothing below changes it. A person trying benchbook on a team repository should not have to commit a tool's config
first, so the declarations can instead be **pinned in the ledger** before the work starts:

```
benchbook init --pack code              # .benchbook/ goes to .git/info/exclude; the [checks] of config.toml are pinned
benchbook check pin                     # pin the working tree's [checks], [guard] and [verify] for the project again
benchbook check pin media --check 'unit=uv run pytest tests/media --junitxml={junit}'   # one piece's own checks
benchbook piece start media --paths 'media/**' --check 'unit=uv run pytest tests/media'  # the same, at the start
```

A pin is a `note` claim tagged `checks-pin` and `piece:<piece>` (`piece:*` for the project). Its payload holds the
checks, the guard paths, the `[verify]` table and the sha256 of the three. Claims are append-only, so a pin is never
edited: a new pin is a new claim, and pinning what the newest pin of that scope already holds records nothing.

When the merge-base has no `.benchbook/config.toml`, `check verify` uses the newest pin of the piece, else the newest
pin of the project, leaving out pins written by any implementer of the piece at that commit: the checks are declared,
append-only, before the work, by someone other than the implementer. A config.toml at the merge-base must declare
valid `[checks]`: with an empty or broken table `check verify` refuses, and no pin is read. The attempt's metrics name the pin (`checks_source = "pin"`,
`checks_pin`, `checks_sha256`), and `checks_changed` is true when:

- the pin was taken from config.toml (`init`, `check pin` without `--check`) and the working tree's config.toml no
  longer hashes to it (an implementer in a linked worktree reads the main checkout's config.toml, so editing it is
  editing the checks), or
- the piece's first attempt ran before the pin in effect now and under no pin or a pin with another hash. A lead may
  re-pin after a failure, and a person then decides that piece once.

A commit that adds its own config.toml still counts as a `[checks]` change. In local mode the lead and the
implementer are often the same person under two names, so a pin protects against an agent's mistake, not against
its user; the team mode (a committed config, CI as the verifier) is the stronger one.

## The verification worktree

`check verify` runs the checks in a fresh detached worktree of the commit, so files git ignores (`.env`,
`.venv`, `node_modules`) are not there. `[verify]` says what the worktree needs first:

```toml
[verify]
copy = [".env"]                 # copied from the repository root to the same place in the worktree
prepare = "uv sync --frozen"    # run in the worktree before the first check
prepare_timeout = 600           # seconds (default 600)
parallel_baseline = false       # true: the merge-base's JUnit run for test counts goes beside the commit's (below)
```

`copy` takes regular files inside the repository that the commit does not track; a path outside it, a symlink, a
missing file or a tracked path fails the preparation. The attempt records the names of the copied files and whether
`prepare` ran (`metrics.prepared`), never their content. When the preparation fails, no check runs: the attempt is
an environment failure (`run_status = "error"`, `metrics.environment = true`), with no link to the completion and
no discarded approach. `[verify]` comes from where the checks come from: the merge-base's config.toml, or the pin.

`parallel_baseline = true` starts the merge-base run that counts JUnit testcases ([Added tests and weakened
tests](#added-tests-and-weakened-tests)) in its own worktree at the same time as the commit's checks, instead of
after them. The verdicts are the same (`tests/test_mutations.py` runs every row both ways); a verify of a piece that
changed guarded files takes about one run of the checks instead of two when the machine has the cores for both.
Leave it off when two copies of the checks would share something: a fixed port, a database, a lock file, a
directory outside the worktree. The early run's result is used only when the commit's run counted exactly the same
checks; an interrupted verify stops it with its own.

### Environment failures

A check can fail in the clean worktree for reasons that are not the commit's: a missing `.env`, a service that is
down. It can also fail at the merge-base by design, because making it pass is the piece's job; a commit that leaves
it failing is a false completion. The two look the same from the clean worktree, so `check verify` asks for evidence
of an environment difference: a failing check is an environment failure only when

1. the implementer's own run of this commit passed it: an attempt of the piece at the commit with `via` set and a
   clean checkout, from `benchbook check run` or the Bash hook, whose author is one of the piece's reporters (or,
   for the Bash hook, whose `metrics.run_by` names one: the agent type it saw), and which ran the same definition
   of the check (`metrics.check_definitions`, a hash of cmd, junit and timeout, equal to the merge-base's). A raw
   Bash pass of a JUnit check does not count, nor does a pass recorded by another agent, of another command under the
   same name, or by a version that recorded no definition. Run `benchbook --agent NAME check run ...` (or set
   `BENCHBOOK_AGENT`) with the name the report goes under, and
2. run again at the merge-base, now, in a clean worktree with the same `[verify]` preparation, it fails the same
   way (the same signature element, or every failing JUnit test fails there too).

When every failing check is an environment failure, the attempt is `run_status = "error"` with
`metrics.environment = true` and `baseline_signature`, and no refutes link or discarded approach is written; the
command says "environment: unit fails the same way at the merge-base a1b2c3d4e5f6; not counted against the piece".
Otherwise the failure refutes the claim. The merge-base run happens only when condition 1 holds, and a commit that
is its own merge-base is not compared. Declaring `[verify] copy` (above) avoids the question for known files.

### Added tests and weakened tests

A piece usually adds tests. `check verify` splits the guarded files a commit changed (`[guard] paths`, by default
`tests/**`, `**/conftest.py` and `.benchbook/config.toml`) by what the change did:

| change | metrics | a person decides? |
| --- | --- | --- |
| a new file, or lines added to a file with none deleted or changed | `guard_added_files` | no |
| names added to an import in a Python file: every deleted line is inside an import statement (`get_path)` → `get_path,` + `run_length)`, `from x import a` → `from x import a, b`) and the file still imports every name, module and alias it did | `guard_added_files` | no |
| any other line deleted or changed (an imported name removed, renamed or re-aliased; an import changed in a file that is not Python or does not parse), a file deleted, a binary change | `guard_weakened_files` | yes |
| an added line with a skip marker (`pytest.mark.skip`, `pytest.mark.xfail`, `pytest.skip(`, `unittest.skip`, `it.skip(`, `describe.skip(`, `test.skip(`, `xit(`, `t.Skip(`, `#[ignore]`, `@Disabled`, `@Ignore`) | `guard_weakened_files` | yes |
| any change to a `conftest.py` (an autouse fixture or hook can weaken every test) | `guard_weakened_files` | yes |
| a JUnit check ran fewer testcases than it did at the merge-base (each check against its own count) | `fewer_tests_checks`, `tests_before`, `tests_after` (sums) | yes |

`guard_weakened` is true for the last four. The all_pass rule, `check verify`'s decision and the PR summary block
on `guard_weakened` and `checks_changed`, not on `guard_touched`, which keeps its meaning (any guarded file
changed); an attempt without `guard_weakened` is read by `guard_touched`. The testcase counts come from a fresh
merge-base run of the JUnit checks in the same environment as the candidate (after the candidate's, or beside it
with `[verify] parallel_baseline`), made when a guarded file changed. The PR summary's "tests changed" column says
"added" and "weakened" apart.

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

## Failure signatures

A failed check is signed so that the same failure gives the same short string in every run, and a different failure a
different one (`src/benchbook/checks/signature.py`, the one place a signature is made). Per failing check, from the most
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
failure with many errors can sign differently there. Runs recorded by older releases keep a rougher signature and
do not match these.

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
- *`check run`* asks only after a failure, never after a pass, so a passing run costs nothing more. The lookup
  opens one plain connection (connect timeout 2 s, statement timeout 1.5 s) in a thread the run waits on for at most
  2 seconds, then prints nothing; any error prints nothing. It runs before the run is queued, so it never finds the
  run itself, and it does not start the embedded server.
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

**A dead end known without a failed run.** `benchbook discard <piece> --approach NAME --why "..." [--commit SHA]`
records the same claim for an approach the lead (or any agent) knows failed, such as one a worker described in a
message or a prompt ruled out: signature `noted: <why>`, payload `noted_by` instead of `first_attempt`, and a source
row for the note (`benchbook-note:sha256:...`) as its supporting evidence. The brief lists it with the others.

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

`benchbook status` counts each piece at its latest active reported commit, open out-of-scope findings, pending
and dead reports, and recent hook failures. If recording needs attention, the brief leads with the reason and
the `benchbook hook replay` command. The spool and the cached `benchbook statusline` refer to the selected
database and writer.

In a code project `benchbook brief` (MCP `benchbook_brief`) is the code pack's
(`benchbook.packs.code.brief:compile_brief`, `brief_compiler` in pack.toml); other harnesses load the same brief
when a session starts ([harnesses.md](harnesses.md)). Narrowed to a module:

```bash
benchbook brief --tags mod:media                     # comma separated; --budget N (default 2000 tokens)
benchbook brief --tags mod:media,file:src/app/modules/media/
benchbook brief --here                               # what this branch changed: file:<dir>/ tags and piece:<branch>
```

It shows, in this order:

1. **Last verified commit per piece**: the newest `verified` completion of each piece in scope, with how it was
   checked (`checked: re-run`, `checked: person`, ...; the oracle of its last decision named by pack.toml
   `[brief.labels]`).
2. **Re-run, not decided** (only when there is one): the newest completion of each piece that a re-run supported or
   refuted and nobody decided, unless a later completion of the piece is verified; most recently re-run first. Each
   line says why: `passed, needs review (tests weakened: <files>)`, `(fewer tests in <check>)` or `([checks]
   changed)` (a person decides, see [the guard](#added-tests-and-weakened-tests)), `failed (<checks>)`, `runs disagree
   (inconclusive)`, or `passed, not decided` (the run did not count, such as a worker's own run).
3. **Reported done, not re-run**: completions still `proposed` with no supporting and no refuting evidence, newest
   first, each `checked: none`. A report is not a check; this section keeps the two apart.
4. **Discarded approaches, do not repeat**: the claims above for the pieces in scope, each with its approach, piece,
   signature, first attempt and number of failed attempts; most recently failed first.
5. **Open out-of-scope findings**: [findings](#out-of-scope-findings) still `proposed` or `inconclusive`, matched by
   tag or by `file:` prefix; most reported first.
6. **Occupied paths**: live pieces ([path occupancy](#path-occupancy)) with their holder, time left, tags and paths.

The footer names `benchbook get <id>` for one record's details (at most 400 tokens).

**Changed since the check.** With `--here` (and from the boundary hooks) the brief compares each line with the
checkout's HEAD and ends it with a mark:

| mark | when |
| --- | --- |
| `· applies at HEAD` | the piece's paths are unchanged between its commit and HEAD |
| `· changed since (N commits)` | N commits since the piece's commit changed its paths |
| `· commit not in this checkout` | this checkout lacks the commit |
| `· not merged into this branch` | the commit exists but is not in HEAD's history (a piece's branch read from the base, or the lead's branch before the merge) |
| `· test changed since` | a discarded approach whose failing test file (the signature before `::`) changed since the commit it failed at; the line moves to the end of its section |

The paths compared (`git diff --quiet <commit> HEAD -- <paths>`) are the files the piece's own commits changed since
the merge-base (`payload.files`, recorded by `check verify` when there are at most 200: a merge commit counts with
the files it changed itself, and a commit merged in from another piece is left out when it is that piece's reported
commit, carries its trailer or has its check runs, and nothing ties it to this piece), else those the piece's newest
task occupies, else the directories its completion recorded. A squash-merged piece reads `applies at HEAD` while
its paths match. Only the lines shown are compared, each git call has 2 seconds, and nothing is stored: the same
ledger at the same HEAD gives the same text. Without a checkout (`--tags`, MCP) the brief shows no marks.

**Scope.**

- A tag matches records that carry it exactly (`mod:media`, `piece:x`); a `file:<prefix>` tag matches by prefix
  the `file:` tags of findings and the paths pieces occupy. With several tags the sections hold the union; without
  `--tags` the brief covers the whole project.
- A piece is in scope when one of its tasks (`piece start`, any status) carries a matching tag or occupies a
  matching path. Discarded approaches and verified commits are found through their piece (a discarded approach also
  through its own `mod:` tags), and also carry the directories their commit changed (`payload.paths` and
  `file:<dir>/` tags, at most 20), so a piece never started with `piece start` is found by path. Findings are
  matched by their own tags only, as in the [query](#reading-open-findings).
- `--here` computes the scope from the checkout: the directories the current branch changed since its merge-base
  with the base ref (`[guard] base`, else `origin/main`) plus the working tree's changes and untracked files, the
  deepest lifted to their parent until at most 20 remain, as `file:` tags, plus `piece:<branch>`. It then adds
  `piece:<piece>` for every piece with a completion at a commit in HEAD's last 5,000 commits, and the Scope line ends
  `+ N pieces merged into HEAD`, so on an integration branch the records of the pieces merged into it belong to it.
  On the base branch itself the scope is empty and the brief covers the whole project. The boundary hooks use the
  same scope.

**Budget.** At most 8 items per section (pack.toml `[brief]`), each line cut to a fixed length; then items are
dropped from the last section upwards until the brief fits `--budget` (with the same 5% slack as the default brief),
and each section says how many it left out (`(+3 more: benchbook piece list)`). Header and footer always stay; a
budget too small for them is an error. A character limit (`max_chars`, 6,000 in the boundary hooks) is applied the
same way: whichever limit is reached first trims. Each brief is logged in `brief_log`.

`--pack` is not an option: the project's pack already chooses the compiler. Other packs refuse `--tags` rather than
ignore it; their briefs are unchanged.

## Reads that changed a plan

**Looking an id up.** Every brief line carries an id; `benchbook get <id>` prints that record in at most 400 tokens:
its kind and id, status and how it was checked (`checked: re-run`, `person`, `none`; pack.toml `[brief.labels]`),
its statement or signature and the payload keys that matter, up to five links each way and the last three decisions,
with what was cut counted ("… 45 more (--json)"). `--json` prints the whole record. Each lookup appends one line to
`lookups.jsonl` in the state directory (time, project, id): an id a brief carried that was then looked up is the
cheapest sign that the brief was read.

A read that changed a plan is counted as a claim id cited by a plan document. Two things are recorded:
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
delegate run it is the number the stop rule reads: after five delegate runs with none, drop signatures and the
brief, and keep verification.

What it can tell: that a plan names a discarded approach, an open finding or a verified commit by its id, and that
the claim was in the ledger before the plan was written. What it cannot: that the id came from a brief rather than
`benchbook search`, `get` or a PR summary (`brief_log` says a brief was compiled, not what it contained, and
`benchbook sql` cannot read it: `Ledger.sql_internal` can); that the citation changed the plan rather than confirmed
it; a read that changed a plan without its id being written down; a plan kept outside files. It counts an upper
bound on citations that came from the brief, and a lower bound on reads that mattered.

## CI evidence and the PR summary

A re-run by CI is a re-run on a machine the implementer does not control. The GitHub Action in
[integrations/github-action](../integrations/github-action/README.md) runs `check verify` for each piece a pull
request completes and keeps one comment on the pull request that says which completions an independent re-run
confirmed. The logic is in three commands, which run anywhere:

```bash
benchbook pr pieces --base BASE [--head HEAD] [--claims] [--whole-as NAME]   # piece<TAB>commit[<TAB>reporter<TAB>claim]
benchbook pr summary --base BASE [--head HEAD] [--run-url URL] [--whole-as NAME]   # the markdown, starting with the marker
benchbook pr comment --repo OWNER/NAME --pr N --body-file F [--author LOGIN] [--update-only]
```

**Pieces.** A commit names the piece it completes with the trailer `Benchbook-Piece: <piece>` (the trailer block at the
end of the message; the key is case-insensitive, and one commit may carry several). For each piece named in `BASE..HEAD`
the piece's commit is the last commit carrying its trailer. A value that is not a piece name (1-100 characters, no
spaces) is skipped with a warning. `--claims` adds the reporter and the id of the completion claim for that piece at
that commit (`-` for none). The reporter is `payload.reported_by` when a lead, a person or a harness agent recorded the
claim, otherwise its author ([Who the implementer is](#who-the-implementer-is)). A claim counts when it carries both
tags and its payload names that piece and commit; with several, a verified one, else the oldest. Passing the id to
`check verify --claim` links the re-run to that very claim, whatever its statement.

**The action's run**, per piece: `check verify <piece> <commit> --base <base sha> --trace-ref '{"ci":
"github-actions", "repository", "run_id", "run_attempt", "run_url", "pull_request"}' --no-decide`, with
`--claim` when a claim exists. Without a claim it records only the attempt, and the piece shows as *not
reported*: CI never reports a completion on someone's behalf. Then `pr summary` and `pr comment`. The job fails when
a re-run failed a check. A pull request rebased after the lead's `check verify` has new commit ids, so its pieces
have no claim at the new commits until someone reports them there.

**Without a ledger** (no `database-url`) the action re-runs the pull request in a ledger it creates for the
workflow run only, and passes `--whole-as pr-<number>` to `pr pieces` and `pr summary`, so the head is re-run as
that piece, reported by the pull request's author, when no trailer names the head commit. The checks still come
only from the config committed at the merge-base ([Set up](#set-up)). What it shows and cannot show: the action's
README, "Without a ledger".

**The summary**, per piece: the state, the runs of its commit (passed, failed, and the independent authors), the
number of discarded approaches (`observation` claims tagged `discarded` and `piece:<piece>`, any status), the open
out-of-scope findings (tagged `out-of-scope` and `piece:<piece>`, status `proposed` or `inconclusive`; a finding
recorded without a piece tag is not counted), and whether tests changed (any attempt of the commit with
`guard_touched` or `checks_changed` true; *unknown* when no attempt says). The states, checked in this order:

| state | when |
| --- | --- |
| **verified** | the claim is `verified`; or no run of the commit failed, none weakened a test or changed `[checks]`, and at least one passing run by an agent other than the implementer (not `via` check run or the hook) satisfies the pack's rule (`Ledger.check_rerun`, the same check `decide ... verified --oracle rerun` makes). The second case is shown with the claim's status: CI records, a lead decides. |
| **re-run disagrees** | the claim is `refuted` or `inconclusive`, or a valid finished run of the commit failed a check (anyone's: the implementer's own failing run counts too). |
| **claimed only** | a claim exists and none of the above: no independent re-run yet, or the commit weakened tests or changed `[checks]`, which a person decides. |
| **not reported** | the ledger has no completion claim for the piece at that commit. |

Piece names, agent names and paths go into the comment as inline code with backticks and `|` neutralised, and the
reasons are escaped so nothing in them renders as HTML, a link or a mention.

**The comment.** `pr comment` lists the pull request's comments with `gh api --paginate` (GH_TOKEN), takes the
first one whose body starts with `<!-- benchbook-summary -->` and, with `--author`, whose author is that login,
updates it, and deletes further marked comments by the same author. With none it creates one, unless
`--update-only` (the action passes it when the pull request names no piece, so pull requests without pieces get
no comment). A marked comment by someone else is never taken for the summary.

**Identity.** Who the action's records are by (`ci:<run_id>` with the owner's URL, the token's agent with a
token), when it may decide, and why the guarantee is against misreporting rather than a pull request written to take
the credential: the action's README, "Who the re-runs are by".

## Out-of-scope findings

An implementer often notices problems outside its piece: a bug in a neighbouring module, a flaky test, a missing
index. With one line in `.benchbook/config.toml` the result-capture hook keeps them as claims the lead can decide
on later, instead of leaving them in a report nobody reads again:

```toml
[report]
out_of_scope_heading = "Out of scope"  # or "범위 밖", or any heading text
agents = ["*"]                         # see Which reports are read
```

Ask the implementers to end their final report with that section, one top-level list item per finding:

```markdown
## Out of scope
- `src/app/modules/media/upload.py`: the retry loop ignores the timeout. mod:media
  - reproduced with tests/media/test_upload.py::test_retry
- The auth token refresh races with logout.
```

Nothing else changes for them: no command to run, no id to cite.

### Which reports are read

The same ones the hook already captures (docs/claude-code.md): the final message of a subagent whose type is in
`BENCHBOOK_HOOK_AGENTS`, or the report it handed back with `SubagentHandback` (then the closing text is not read
again). Findings are recorded only in a project created with `--pack code` and only while the heading is set. The
heading is read when the hook fires and kept with the queued report, so `benchbook hook replay` looks for the
section the run had. The hook's background processor parses the section and writes the claims; the hook process
only queues the report and exits. A report that cites its own records gets its findings recorded too (the note is
what such a report skips).

`[report] agents` widens the reports read for findings without making them workers: a list of subagent types
(with or without a plugin prefix), or `["*"]` for every subagent. While `BENCHBOOK_HOOK_AGENTS` is unset, the
final report of a listed subagent is queued for its findings only: no `[auto-captured]` note is written for it.
`BENCHBOOK_HOOK_AGENTS`, when set, still decides alone. `benchbook init --pack code` writes the `[report]` table
above when config.toml has none yet.

### What counts as a finding

- **The section**: a markdown heading of any level, or a line that is bold as a whole (`**Out of scope**`,
  `**Out of scope:**`), whose text equals the configured heading after NFKC normalisation, case folding,
  whitespace collapsing and dropping a trailing colon. A heading section ends at the next heading of the same or a
  higher level (deeper headings inside it are group labels); a bold-line section ends at the next heading of any
  level or the next bold line. A thematic break (`---`) ends either. Every such section in the report is read.
- **One line**: outside a section, a line `<heading>: <text>` (also as a list item, with the heading in bold, or
  with a full-width colon) is one finding whose text is what follows the colon. `Out of scope: none` is nothing.
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
           piece:<piece>     when the run recorded attempts (exp run / exp record) of exactly one piece; else
                             the piece the report's commit names (DONE <sha> or commit: <sha>, then its
                             Benchbook-Piece trailer, a piece: line or the one branch at it), the piece the
                             automatic re-run of the same report verifies
           mod:<name>        each mod:<name> the item's text writes itself (never inferred from a path)
           file:<path>       each path the finding names, repository-relative, up to 10
payload    {"text", "details", "file", "piece", "heading",
            "source": {"session_id", "event", "agent_id", "agent_type", "tool_name"}}
author     hook (the agent of BENCHBOOK_TOKEN when the hook runs under a token)
```

`payload.source` says which subagent reported the finding, also when the author is a token's agent.
`payload.file` is the first path, the one the dedup key uses. The report itself is stored once as a source
(`uri` `subagent-report:sha256:<sha256 of the report text>`, title naming the subagent type, agent and session) and
linked `supports` to every finding it names. The report text itself stays in the auto-captured note.

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

`benchbook finding add "..." [--file PATH] [--piece P]` records a finding the lead found itself (or one a worker
gave in a message) as a one-item "Out of scope" report, with the same key and dedup as a report's, and a
`finding noted by <agent>` source.

`benchbook finding accept` exists because no other command links a follow-up issue: it adds the issue URL as a source
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
-  It never waits for the database. The result goes to the local hook spool (one directory per database target and
  writer, as for the result-capture hook) and the spool's background processor records it. A database that is down
  cannot change the exit code or lose the result: the entry waits in the spool and `benchbook hook replay` records it.
  The time it adds to a check is at most 300 ms (`tests/test_check_run.py` measures it against the bare command).

The attempt has check verify's shape ([What is recorded](#what-is-recorded)), with `code_ref` the HEAD the check
ran at, notes naming the checkout, and two more metrics, `"via": "check-run"` and `"worktree_dirty": false`:

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

The share of declared check runs that reached the ledger (target 95%): ledger attempts over the runs the
Claude Code hook observed.

- **Denominator.** Every Bash call the hook matches to a declared check appends one line to `observed.jsonl` in the
  spool directory of the database target and writer (`~/.local/state/benchbook/spool/<key>/`; `benchbook doctor`
  prints the path on its `hook failures` line). A line has `ts`, `project`, `piece`, `check`, `kind`,
  `exit_code`, `reason`, `session_id`, `tool_use_id` and `harness` (`claude-code`, `codex`, `cursor`, `gemini`,
  `opencode` or `pi`; docs/harnesses.md; Cursor's lines add `harness_version`). `kind` is `direct`, `wrapped` or
  `unrecordable`, with the `reason` of an unrecordable run (docs/claude-code.md, "`benchbook hook bash-result`").
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
