# Coding work: re-runs and what they record

Part of [the code pack](code.md).

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
[the numbers](code.md#the-numbers) either, and `check verify` decides nothing on it.

The completion claim: statement `<piece> is complete at <sha12>`, tags `piece:<piece>` and `commit:<sha>`,
payload `{"piece", "commit", "reported_checks"}` and, with `--report-file`, `"report": {"sha256", "bytes"}`.
`reported_checks` lists the declared check names the report mentions (a mention, not a claimed result).

A claim's identity is the triple (piece, commit, reporter), not its statement. `check verify` reuses a claim only
when it carries both tags, its payload names the same piece and commit, and it records this reporter's report,
whatever its statement: its reporter is `--reported-by` (see [Who the implementer is](code.md#who-the-implementer-is))
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

When the merge-base has no config, `check verify` uses the newest pin of the piece, else the newest pin of the project,
leaving out pins written by any implementer of the piece at that commit: the checks are declared, append-only, before
the work, by someone other than the implementer. The attempt's metrics name the pin (`checks_source = "pin"`,
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
