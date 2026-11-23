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

`junit = true` is also what makes "nothing ran" visible. An exit code alone cannot tell a passing test run from
one that stopped before any test (code outside the guarded paths that ends the process with status 0 while the tests
are collected, a filter that selects nothing). With `junit = true`, exit code 0 is a pass only when the JUnit file
exists, holds at least one testcase that ran (not skipped), and none of them failed or erred; otherwise the check
fails with the part `exit 0, no JUnit report` or `exit 0, no tests ran` (or the failing test's). `check verify`
and `check run` both apply it (`check run` still exits with the command's code and records the failure). A check
without `junit` is judged by its exit code only. The JUnit file is written by the code under test's own process,
so this catches a run that ended early, not a commit written to forge the report.

Guard globs are matched against the whole repository-relative path: `*`, `?` and `[...]` stay inside one
path segment, a `**` segment spans any number of them (`tests/**` is every file under `tests/`, `**/conftest.py`
is a conftest.py at any depth, the root included). Without `[guard] paths` the three globs above are used.

## The lead's line

With the Claude Code hook plugin, a coding agent (foreground, or in the background as Claude Code runs every Agent
call by default) can report its commit with a final `DONE <sha>`
line, a `commit: <sha>` or `커밋: <sha>` line, or a single unambiguous full SHA. Benchbook takes the piece
from the commit's `Benchbook-Piece:` trailer, the report's `piece:` line, or a unique local branch pointing to
that commit, in that order. The processor runs the same declared checks in a clean worktree and sends the
result back to the parent session. A completion that needs human review includes that information in the note.

In the integration step, for each piece an implementer reported done:

```bash
benchbook check verify <piece> <commit> --reported-by <implementer>
```

Options: `--base REF` (default `[guard] base`, else `origin/main`), `--checks a,b` (a subset of the declared
checks: the attempt is recorded as partial and never verifies the completion, see below), `--claim ID` (the
completion claim the re-run is evidence for, as `pr pieces --claims` prints it: it must be about the piece and
commit, and its reporter is the implementer, so `--reported-by` may be left out), `--report-file F` (the implementer's report), `--trace-ref JSON` (a JSON object, or `@file`, stored as the
attempt's `trace_ref`: the CI action puts its run's URL there), `--no-decide` (record only, even with lead
authority). It prints one row per check and exits 0 only when every check passed, 1 otherwise; `--json` gives the
same as one JSON object.

What it does:

1. Resolves `<commit>` to a full sha.
2. Finds the merge-base of the base and the commit, lists the files changed between them (`git diff --name-only
   --no-renames`, so a deleted or moved-away test shows up under its old path) and keeps those matching the guard
   globs as `guard_files`. `checks_changed` says whether the `[checks]` table of the commit's config.toml differs
   from the merge-base's (parsed, so comments and layout do not count).
3. Runs the checks **declared at the merge-base** in a detached `git worktree` of the commit, each through the core
   runner with its timeout. A commit that rewrites a check's command still gets the merge-base's command, and the
   change shows up as `checks_changed`. The worktree and its git metadata are removed afterwards, also when a check
   times out or the command is interrupted. The checks run with the verifier's environment minus the ledger's
   credentials: `BENCHBOOK_TOKEN`, `BENCHBOOK_DATABASE_URL`, `BENCHBOOK_READER_PASSWORD`, their `ASSAY_` forms,
   `PGPASSWORD` and `PGPASSFILE` are removed (the code under test is the implementer's, and must not write as the
   verifier); everything else, `PATH` and the checks' own variables included, is kept. A check that needs a database
   password takes it from a variable of another name. `check run` runs in the implementer's own environment.
4. Records the completion claim of the report (status `proposed`; in the implementer's name, or under a token in
   the verifier's name with `payload.reported_by`; see [the completion claim](#what-is-recorded)), the attempt in
   the verifier's name, and a `supports` (every check passed) or `refutes` link from the attempt to the claim. An
   interrupted run is recorded with `run_status = "error"` and no link.
5. Decides, only with lead authority: see [Verified completion](#verified-completion).
6. Prints the table and the decision.

The verifier is `--agent` or `BENCHBOOK_AGENT` when set (with `BENCHBOOK_TOKEN`, the token's agent), else
`verifier@<host>` (registered with the harness role). It must be a different agent from `--reported-by`; the command refuses to verify an agent's own report.
The hook's automatic re-run of a reported `DONE <sha>` runs as another name, `rerun@<host>` (registered with the
orchestrator role), or as the token's agent under `BENCHBOOK_TOKEN`.

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
  `--lease` is given. Both renew only a live lease, read under the same lock the overlap check holds, and every
  lease (a new piece's, a renewal's) counts from the database's clock after that lock, not from the transaction's
  start: once it has run out, another piece may hold the paths, so a heartbeat fails with "lease lost", and
  `piece start <piece>` starts a new piece through the overlap check. Without `--paths` it takes the paths the
  expired piece declared, also when a `next_task` has since marked it failed; a piece released with `piece done`
  (even while a renewal was under way) needs `--paths` again.
  Reading the current generation and renewing, restarting, or releasing it happen in the same transaction under the
  project intent lock. If a restart acquires the lock first, `piece done` can see and release the new generation.
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
`settings.worker_intents` (JSON `true`), which `init --pack code` sets; in an older code project a lead turns it on
with `Ledger.update_settings({"worker_intents": True})`, or starts the pieces itself. The database enforces that
setting for a worker's intent, and caps every lease a token sets (an intent, a renewal, a claim) at
`settings.max_lease_seconds` (default 86400).

**A limit under tokens:** the overlap check (piece name and paths) runs in the SDK, not in the database. A worker
that writes the task row with its own SQL on the writer login can still take a piece name or paths another agent
holds, for at most the lease cap; the row is in its name, and `piece list` and `v_inflight` show it. Path occupancy
prevents accidental collisions between agents that use benchbook, not a writer set on causing one.

From the SDK the same check is available to any intent: `create_intent(..., exclusive_paths=["paths"])` and
`update_intent(..., exclusive_paths=["paths"])` refuse when the named spec field (a list of globs) overlaps the same
field of a live intent.

## What is recorded

One attempt, in the shape of spec §6:

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
random `statement_hash`. So nobody can take a completion first: a claim with the usual statement and the right payload
does not give its author the credit, and one with a wrong commit in its payload does not block the piece. Older
ledgers need nothing: their claims have the usual statement and are found the same way.

`statement_hash` is computed by the client (`dedup.statement_hash`, or a caller's own hash for discarded approaches
and findings). The database checks only that it is unique in the project; under a token, a writer can store any
hash with any statement. Nothing reads a claim's identity from it: that is an SDK rule, and the triple above is
what `check verify` and `pr` match on. A check in the database that the hash fits the statement is not possible
without refusing the callers that supply their own hash, and would not stop anyone from writing the usual statement
itself.

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
  `min_authors - 1` such agents, each with every name in `checks_declared` among its `checks` (not partial),
  `checks_passed == checks_total`, `checks_total > 0`, and one `ok` result per counted check. Both
  `guard_touched` and `checks_changed` must be stored as `false`;
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

The automatic re-run of a subagent's report (`benchbook hook agent-result`) decides as `rerun@<host>`, which is
registered with the orchestrator role; under `BENCHBOOK_TOKEN` it is the token's agent with the token's role. The
two default names are separate because a project keeps the role a name was first registered with: before, both
used `verifier@<host>`, and whichever ran first decided whether the other could decide. A ledger where
`verifier@<host>` was already registered as an orchestrator keeps that row; nothing is migrated. When the re-run
passed and still decided nothing, its line says why after `checked: none`, for example
`checked: none (recorded by a worker agent; a lead decides)`.

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

When the merge-base has no config, `check verify` uses the newest pin of the piece, else the newest pin of the
project, leaving out pins written by any implementer of the piece at that commit. The integrity argument moves from
"committed at the merge-base" to "declared, append-only, before the work, by someone other than the implementer". The
attempt's metrics name the pin (`checks_source = "pin"`, `checks_pin`, `checks_sha256`), and `checks_changed` is
true when:

- the pin was taken from config.toml (`init`, `check pin` without `--check`) and the working tree's config.toml no
  longer hashes to it (an implementer in a linked worktree reads the main checkout's config.toml, so editing it is
  editing the checks), or
- the piece's first attempt ran before the pin in effect now and under no pin or a pin with another hash. A lead may
  re-pin after a failure, and a person then decides that piece once.

A commit that adds its own config.toml still counts as a `[checks]` change, as before. In local mode the lead and
the implementer are often the same person under two names, so a pin protects against an agent's mistake, not
against its user; the team mode (a committed config, CI as the verifier) is the stronger one.

## The verification worktree

`check verify` runs the checks in a fresh detached worktree of the commit, so files git ignores (`.env`,
`.venv`, `node_modules`) are not there. `[verify]` says what the worktree needs first:

```toml
[verify]
copy = [".env"]                 # copied from the repository root to the same place in the worktree
prepare = "uv sync --frozen"    # run in the worktree before the first check
prepare_timeout = 600           # seconds (default 600)
```

`copy` takes regular files inside the repository that the commit does not track; a path outside it, a symlink, a
missing file or a tracked path fails the preparation. The attempt records the names of the copied files and whether
`prepare` ran (`metrics.prepared`), never their content. When the preparation fails, no check runs: the attempt is
an environment failure (`run_status = "error"`, `metrics.environment = true`), with no link to the completion and
no discarded approach. `[verify]` comes from where the checks come from: the merge-base's config.toml, or the pin.

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
Otherwise the failure refutes as before. The merge-base run happens only when condition 1 holds, and a commit that is
its own merge-base is not compared. Declaring `[verify] copy` (above) avoids the question for known files.

### Added tests and weakened tests

A piece usually adds tests. `check verify` splits the guarded files a commit changed (`[guard] paths`, by default
`tests/**`, `**/conftest.py` and `.benchbook/config.toml`) by what the change did:

| change | metrics | a person decides? |
| --- | --- | --- |
| a new file, or lines added to a file with none deleted or changed | `guard_added_files` | no |
| lines deleted or changed, a file deleted, a binary change | `guard_weakened_files` | yes |
| an added line with a skip marker (`pytest.mark.skip`, `pytest.mark.xfail`, `pytest.skip(`, `unittest.skip`, `it.skip(`, `describe.skip(`, `test.skip(`, `xit(`, `t.Skip(`, `#[ignore]`, `@Disabled`, `@Ignore`) | `guard_weakened_files` | yes |
| any change to a `conftest.py` (an autouse fixture or hook can weaken every test) | `guard_weakened_files` | yes |
| a JUnit check ran fewer testcases than it did at the merge-base (each check against its own count) | `fewer_tests_checks`, `tests_before`, `tests_after` (sums) | yes |

`guard_weakened` is true for the last four. The all_pass rule, `check verify`'s decision and the PR summary block
on `guard_weakened` and `checks_changed`, not on `guard_touched`, which keeps its meaning (any guarded file
changed). An attempt recorded before `guard_weakened` existed is read by `guard_touched`, as before. The testcase
counts come from a fresh merge-base run of the JUnit checks in the same environment as the candidate. This
comparison runs when a guarded file changed. The PR summary's "tests changed" column says "added" and "weakened" apart.

## The numbers

Run `uv run python -m bench.boundary.run --dry` to generate a synthetic H1–H6 report for the boundary
benchmark. For the fan-in experiment, the circle-packing runner accepts `--workers 8,16,30`.
[Boundary benchmark preregistration](../bench/boundary/PREREGISTRATION.md)

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

- `completions`: reported completions (one claim per piece, commit and reporter). `rerun`: those with at least one finished
  re-run linked to them: a pass of every declared check, or a failure (a pass of only some checks is not linked). `false_completions`: those with at least one re-run in which a check failed. The rate is over `rerun`;
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

`benchbook status` counts each piece at its latest active reported commit, open out-of-scope findings, pending
and dead reports, and recent hook failures. If recording needs attention, the brief leads with the reason and
the `benchbook hook replay` command. The spool and the cached `benchbook statusline` refer to the selected
database and writer.

Codex, Cursor, Gemini CLI, OpenCode V2 and Pi can load the same scoped brief when a session starts and deliver
pending notes after tool use. [Harness setup and event support](harnesses.md)

In a code project `benchbook brief` (MCP `benchbook_brief`) is the code pack's
(`benchbook.packs.code.brief:compile_brief`, `brief_compiler` in pack.toml). Narrowed to a module:

```bash
benchbook brief --tags mod:media                     # comma separated; --budget N (default 2000 tokens)
benchbook brief --tags mod:media,file:src/app/modules/media/
benchbook brief --here                               # what this branch changed: file:<dir>/ tags and piece:<branch>
```

It shows, in this order:

1. **Last verified commit per piece**: the newest `verified` completion of each piece in scope, with how it was
   checked (`checked: re-run`, `checked: person`, ...; the oracle of its last decision named by pack.toml
   `[brief.labels]`).
2. **Reported done, not re-run**: completions still `proposed` with no supporting and no refuting evidence, newest
   first, each `checked: none`. A report is not a check; this section keeps the two apart.
3. **Discarded approaches, do not repeat**: the claims above for the pieces in scope, each with its approach, piece,
   signature, first attempt and number of failed attempts; most recently failed first.
4. **Open out-of-scope findings**: [findings](#out-of-scope-findings) still `proposed` or `inconclusive`, matched by
   tag or by `file:` prefix; most reported first.
5. **Occupied paths**: live pieces ([path occupancy](#path-occupancy)) with their holder, time left, tags and paths.

The footer names `benchbook get <id>` for one record's details (at most 400 tokens).

**Changed since the check.** With `--here` (and from the boundary hooks) the brief compares each verified line with
the checkout's HEAD: `git diff --quiet <commit> HEAD -- <paths>`, where the paths are those the piece's newest task
occupies (`piece start`), else the directories its completion recorded. A line whose paths changed ends in
`· changed since (2 commits)` (the commits since that touch those paths); one whose commit this checkout lacks ends
in `· commit not in this checkout`; an unchanged line has no mark. A discarded approach whose failing test file
(the part of the signature before `::`) changed since the commit it failed at ends in `· test changed since` and
moves to the end of its section: the failure may no longer hold. Only the lines shown are compared (at most 8 per
section), each git call has 2 seconds, and nothing is stored: the same ledger at the same HEAD gives the same text.
Without a checkout (`--tags`, MCP) the brief shows no marks.

**Scope.** A tag matches records that carry it exactly (`mod:media`, `piece:x`). A `file:<prefix>` tag matches by
prefix: the `file:` tags of findings and the paths pieces occupy. A piece is in scope when one of its tasks (`piece
start`, any status) carries a matching tag or occupies a matching path; discarded approaches and verified commits
are found through their piece (and a discarded approach also through its own `mod:` tags). Findings are matched by
their own tags only, as in the [query](#reading-open-findings). Discarded approaches and completions also carry the
directories their commit changed since the merge-base (`payload.paths` and `file:<dir>/` tags, at most 20), so a
piece never started with `piece start` is found by path, and a `piece:<name>` tag matches them directly.

**`--here`** computes the scope from the checkout: the directories the current branch changed since its merge-base
with the base ref (`[guard] base`, else `origin/main`) plus the working tree's changes and untracked files, each
file reduced to its directory and the deepest lifted to their parent until at most 20 remain, as `file:` tags, plus
`piece:<branch>`. On the base branch itself the scope is empty and the brief covers the whole project. The boundary
hooks use the same scope. Without `--tags` the brief covers the whole project. With several tags
the sections hold the union.

**Budget.** At most 8 items per section (pack.toml `[brief]`), each line cut to a fixed length; then items are
dropped from the last section upwards until the brief fits `--budget` (with the same 5% slack as the default brief),
and each section says how many it left out (`(+3 more: benchbook piece list)`). Header and footer always stay; a
budget too small for them is an error. A character limit (`max_chars`, 6,000 in the boundary hooks) is applied the
same way: whichever limit is reached first trims. Each brief is logged in `brief_log` as before.

`--pack` is not an option: the project's pack already chooses the compiler. Other packs refuse `--tags` rather than
ignore it; their briefs are unchanged.

## Reads that changed a plan

**Looking an id up.** Every brief line carries an id; `benchbook get <id>` prints that record in at most 400 tokens:
its kind and id, status and how it was checked (`checked: re-run`, `person`, `none`; pack.toml `[brief.labels]`),
its statement or signature and the payload keys that matter, up to five links each way and the last three decisions,
with what was cut counted ("… 45 more (--json)"). `--json` prints the whole record. Each lookup appends one line to
`lookups.jsonl` in the state directory (time, project, id): an id a brief carried that was then looked up is the
cheapest sign that the brief was read.

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
## CI evidence and the PR summary

A re-run by CI is a re-run on a machine the implementer does not control. The GitHub Action in
[integrations/github-action](../integrations/github-action/README.md) runs `check verify` for each piece a pull
request completes and keeps one comment on the pull request that says which completions an independent re-run
confirmed. The logic is in three commands, which run anywhere:

```bash
benchbook pr pieces --base BASE [--head HEAD] [--claims]   # piece<TAB>commit[<TAB>reporter<TAB>claim], one per piece
benchbook pr summary --base BASE [--head HEAD] [--run-url URL]   # the markdown, starting with the marker
benchbook pr comment --repo OWNER/NAME --pr N --body-file F [--author LOGIN] [--update-only]
```

**Pieces.** A commit names the piece it completes with the trailer `Benchbook-Piece: <piece>` (the trailer block at
the end of the message; the key is case-insensitive, and one commit may carry several). For each piece named in
`BASE..HEAD` the piece's commit is the last commit carrying its trailer. A value that is not a piece name (1-100
characters, no spaces) is skipped with a warning. `--claims` adds the reporter and the id of the completion claim
for that piece at that commit (`-` for none). The reporter is `payload.reported_by` when a lead, a person or a harness agent recorded the
claim, otherwise its author ([Who the implementer is](#who-the-implementer-is)). A claim counts when it carries both
tags and its payload names that piece and commit; with several, a verified one, else the oldest. Passing the id to
`check verify --claim` links the re-run to that very claim, whatever its statement.

**The action's run**, per piece: `check verify <piece> <commit> --base <base sha> --trace-ref '{"ci":
"github-actions", "repository", "run_id", "run_attempt", "run_url", "pull_request"}' --no-decide`, with
`--claim` when a claim exists. Without a claim it records only the attempt, and the piece shows as *not
reported*: CI never reports a completion on someone's behalf. Then `pr summary` and `pr comment`. The job fails when
a re-run failed a check. A pull request rebased after the lead's `check verify` has new commit ids, so its pieces
have no claim at the new commits until someone reports them there.

**The summary**, per piece: the state, the runs of its commit (passed, failed, and the independent authors), the
number of discarded approaches (`observation` claims tagged `discarded` and `piece:<piece>`, any status), the open
out-of-scope findings (tagged `out-of-scope` and `piece:<piece>`, status `proposed` or `inconclusive`; a finding
recorded without a piece tag is not counted), and whether tests changed (any attempt of the commit with
`guard_touched` or `checks_changed` true; *unknown* when no attempt says). The states, checked in this order:

| state | when |
| --- | --- |
| **verified** | the claim is `verified`; or no run of the commit failed, none touched a guarded path or changed `[checks]`, and at least one passing run by an agent other than the implementer (not `via` check run or the hook) satisfies the pack's rule (`Ledger._check_rerun`, the same check `decide ... verified --oracle rerun` makes). The second case is shown with the claim's status: CI records, a lead decides. |
| **re-run disagrees** | the claim is `refuted` or `inconclusive`, or a valid finished run of the commit failed a check (anyone's: the implementer's own failing run counts too). |
| **claimed only** | a claim exists and none of the above: no independent re-run yet, or the commit changed tests, which a person decides. |
| **not reported** | the ledger has no completion claim for the piece at that commit. |

Piece names, agent names and paths go into the comment as inline code with backticks and `|` neutralised, and the
reasons are escaped so nothing in them renders as HTML, a link or a mention.

**The comment.** `pr comment` lists the pull request's comments with `gh api --paginate` (GH_TOKEN), takes the
first one whose body starts with `<!-- benchbook-summary -->` and, with `--author`, whose author is that login,
updates it, and deletes further marked comments by the same author. With none it creates one, unless
`--update-only` (the action passes it when the pull request names no piece, so pull requests without pieces get
no comment). A marked comment by someone else is never taken for the summary.

**Identity.** With the owner's URL the action writes as `ci:<run_id>`: a self-declared worker name, one per workflow
run, which any holder of the URL could also use. With a `benchbook_writer` login and a token every record is the
token's agent's (a token is one agent, so all runs share it; the run is in `trace_ref`). The action passes
`--no-decide` unless its `decide` input is `true` and it holds a token; check verify then decides only if the
token's role is orchestrator or human. In both modes the checks run the pull request's code in the job that holds
the credential. `check verify` removes the ledger's credentials from the checks' environment, but the code runs as
the same user as the verifier, so the guarantee is against misreporting, not against a pull request written to take
the credential; the action's README says what follows from that.

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

`[report] agents` widens the reports read for findings without making them workers: a list of subagent types
(with or without a plugin prefix), or `["*"]` for every subagent. While `BENCHBOOK_HOOK_AGENTS` is unset, the
final report of a listed subagent is queued for its findings only: no `[auto-captured]` note is written for it.
`BENCHBOOK_HOOK_AGENTS`, when set, still decides alone. `benchbook init --pack code` writes
`out_of_scope_heading = "Out of scope"` and `agents = ["*"]` when config.toml has no `[report]` table yet.

### What counts as a finding

- **The section**: a markdown heading of any level, or a line that is bold as a whole (`**범위 밖**`,
  `**Out of scope:**`), whose text equals the configured heading after NFKC normalisation, case folding,
  whitespace collapsing and dropping a trailing colon. A heading section ends at the next heading of the same or a
  higher level (deeper headings inside it are group labels); a bold-line section ends at the next heading of any
  level or the next bold line. A thematic break (`---`) ends either. Every such section in the report is read.
- **One line**: outside a section, a line `<heading>: <text>` (also as a list item, or with the heading in bold,
  `**범위 밖:** ...`, and with a full-width colon) is one finding whose text is what follows the colon. `범위 밖: 없음`
  is nothing.
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
metrics    {"checks_passed": 1, "checks_total": 1, "checks": {"pytest-media": "ok"},
            "checks_declared": ["pytest-media"], "failure_signature": null,
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
