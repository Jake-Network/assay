# benchbook evidence for pull requests (GitHub Action)

A composite action that re-runs, on every pull request, the declared checks of each piece of coding work the pull
request's commits complete, records the re-runs in the shared benchbook ledger, and keeps **one** comment on the
pull request saying, per piece, whether an independent re-run confirmed the completion. A reviewer sees which
completions are verified without opening the ledger. What is recorded, and what *verified* means, is in
[docs/code.md](../../docs/code.md#ci-evidence-and-the-pr-summary).

## What it does

1. Finds the pieces: every commit of the pull request (base..head) whose message ends with the trailer
   `Benchbook-Piece: <piece>`. The last commit carrying a piece's trailer is that piece's commit. Ask implementers
   to end their commit message with it, or add it with `git commit --trailer "Benchbook-Piece: media-upload"`.
2. For each piece, runs `benchbook check verify <piece> <commit> --base <the pull request's base>` with the
   workflow run's URL in the attempt's `trace_ref`. When the ledger already has a completion claim for that piece
   and commit, the re-run is linked to that claim (`--claim` its id, from `pr pieces --claims`); otherwise only the
   attempt is recorded.
3. Writes the summary (`benchbook pr summary`) to the job summary and creates or updates the comment that starts
   with `<!-- benchbook-summary -->` (`benchbook pr comment`). Running again on the same pull request updates that
   comment; extra copies left by two runs racing each other are deleted.
4. Exits 1 when a re-run failed a check (or a step could not run), 0 otherwise.

Per piece the comment shows its state (**verified**, **claimed only**, **re-run disagrees**, or **not reported**
when nobody reported the completion), the runs of its commit, the number of discarded approaches, open
out-of-scope findings, and whether the commit changed tests (guarded paths or `[checks]`).

## Sample workflow

```yaml
# .github/workflows/benchbook.yml
name: benchbook
on:
  pull_request:          # never pull_request_target: the checks run the pull request's code
permissions:
  contents: read
  pull-requests: write   # the summary comment
concurrency:             # one run per pull request at a time, so two runs do not both create the comment
  group: benchbook-${{ github.event.pull_request.number }}
  cancel-in-progress: false
jobs:
  evidence:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1
        with:
          fetch-depth: 0               # the base, the merge-base and every commit of the pull request
          persist-credentials: false   # the checks run the pull request's code; leave no token in .git/config
      # Pin to a full commit sha of the benchbook repository, not a branch or tag.
      - uses: Jake-Network/benchbook/integrations/github-action@<full commit sha>
        with:
          database-url: ${{ secrets.BENCHBOOK_DATABASE_URL }}
          token: ${{ secrets.BENCHBOOK_TOKEN }}       # optional; see "Who the re-runs are by"
```

The checks run as `.benchbook/config.toml` declares them at the merge-base (docs/code.md), from the repository
root, so the job needs whatever they need (a language runtime, `uv`, services). Add those steps before this one.
benchbook needs Python 3.11 or later; `ubuntu-latest` has it, elsewhere add `actions/setup-python` first.

**Pinning.** GitHub recommends pinning every action to a full-length commit sha, which a later push cannot move. The
sha above is actions/checkout's v7.0.1 as of 2026-11-21; check it against that repository before use. By default
this action installs benchbook from its own tree (the commit you pinned), so the commands always match the action;
`benchbook: benchbook==X.Y.Z` installs a release from PyPI instead.

**Branch protection.** To keep a pull request whose re-run failed from merging, make this job a required status
check (Settings, Branches, branch protection rule or ruleset, "Require status checks to pass", job `evidence`).
That is the CI row of spec §7: the completion is blocked by the branch rule, not by the agent. A *claimed only*
piece (no independent re-run yet, or tests changed) does not fail the job; the comment says why, and a person
decides. A pull request from a fork passes without a re-run (below), so a required check alone does not verify a
fork's pieces.

## Inputs

| input | default | what |
| --- | --- | --- |
| `database-url` | | The ledger's Postgres URL, from a secret. Never written to the log or given to `gh`. |
| `token` | | A per-agent token (`bbt_...`) for a `benchbook_writer` login. |
| `project` | config.toml | The ledger project. |
| `decide` | `false` | `true` lets check verify decide when the token is a lead's (below). |
| `comment` | `true` | `false` writes only the job summary. |
| `comment-author` | `github-actions[bot]` | Only a marked comment by this login is taken for the summary; set it to your bot's login with another `github-token`. |
| `github-token` | `github.token` | For the comment; given to `gh` only, never to the checks. |
| `benchbook` | (this tree) | A pip requirement, or `skip` when benchbook is already on PATH. |
| `python` | `python3` | The interpreter benchbook is installed with. |
| `working-directory` | `.` | The checkout with `.benchbook/config.toml`. |

## Without a ledger

Without `database-url` the action still adds to your CI's test run the one thing a test run does not show: whether
the pull request weakened the tests to get green (an assert deleted or changed, a skip marker, a conftest change,
fewer tests run, or a changed `[checks]` table). It re-runs the checks declared at the merge-base, from a clean
worktree of the pull request's head, in a ledger it creates for this workflow run only (an embedded Postgres under
`RUNNER_TEMP`, project `pr-<number>` unless `project` is set, stopped at the end; the checkout is not touched). No
secret, no shared Postgres, no commit trailer. The log says so in a notice, so a workflow whose `database-url`
secret is missing by mistake is visible.

What it needs: `.benchbook/config.toml` committed on the base branch, and one workflow file. `benchbook init` keeps
`.benchbook/` out of git (`.git/info/exclude`), so commit that one file by name:

```bash
benchbook init                          # proposes and writes the checks
git add -f .benchbook/config.toml       # the project line and the [report] table go in with [checks] and [guard]
git commit -m "Declare the checks benchbook re-runs"
```

The checks come only from the config committed at the merge-base, so a pull request cannot choose its own checks.
The pull request that first adds the config therefore ends with a notice ("no config committed at the
merge-base"); the pull requests after it is merged get the comment.

The workflow is the [sample](#sample-workflow) without the `database-url` and `token` lines:

```yaml
jobs:
  evidence:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1
        with:
          fetch-depth: 0
          persist-credentials: false
      - uses: Jake-Network/benchbook/integrations/github-action@<full commit sha>
```

**What the comment shows.** Each `Benchbook-Piece:` trailer's piece is its own row, as with a shared ledger, and the
pull request's head is always re-run: when no trailer names the head commit (no trailers at all, or a fix after the
last trailered commit) it is the row `pr-<number>`, reported by the pull request's author. A run that has nothing to
re-run (no config at the merge-base, for example after the base branch changed) updates an earlier comment to say so. Per piece: **verified** when every declared check passed in the re-run and no test was
weakened; **claimed only** with `tests weakened: <files>` when the commit touched a guarded test in a way that can
weaken it (a person decides; the job does not fail for it); **re-run disagrees** when a check failed (the job
fails). The comment ends with a paragraph saying that this run had no shared ledger.

**What it cannot show.** Nothing from agent sessions: discarded approaches, out-of-scope findings and earlier runs
live in a shared ledger, and this one is gone when the job ends. `decide` and `token` are ignored with a warning.

**Trust.** As with a shared ledger, the checks run the pull request's code in the same job, as the same user, that
writes the comment: a pull request written to do so can change what the comment says. The comment is evidence
against agents that misreport their work, not against a hostile pull request. A fork's pull request is skipped as
before.

## Pull requests from forks, and Dependabot

GitHub gives a workflow run for a fork's pull request no secrets and a read-only `GITHUB_TOKEN`, and treats
Dependabot's pull requests the same way. The action then prints a notice saying so and exits 0: nothing is re-run,
recorded or commented, and the pull request is not failed for it. A same-repository pull request without
`database-url` runs with a ledger of its own ([Without a ledger](#without-a-ledger)).

## Who the re-runs are by

CI is useful as a verifier because an implementer cannot run it under its own name. How far that holds depends on
the credential in the secret:

| secret | author of the records | what the ledger guarantees |
| --- | --- | --- |
| the owner's URL (local style) | `ci:<run_id>`, a new worker agent per workflow run | nothing about names: the name is self-declared, and any process with the same URL can write as `ci:...` too. The owner URL also lets its holder migrate and register agents. |
| a `benchbook_writer` login and a token | the token's agent, for every run (a token is one fixed agent); the run id and URL are in each attempt's `trace_ref` | no other token can write as that agent, and it can do only what its role allows. |

For the token, register one agent for CI with the harness role and store its token as `BENCHBOOK_TOKEN`:

```bash
benchbook agent add ci --role harness        # on the operator's (owner) connection; prints the token once
```

**Deciding.** By default the action passes `--no-decide`: CI records its re-runs and links them to the
completion claims, and a lead decides (the comment already says *verified* when the code pack's rule would accept
the recorded runs). With `decide: true` and a lead's token (orchestrator or human role), check verify decides as it
does for a lead (verified, or inconclusive when runs disagree). With an owner URL, `decide: true` is ignored with a
warning: a self-declared name is no authority. A harness or worker token never decides.

**What no setting covers.** The job runs the pull request's code (the checks) in the same job as the credential.
`check verify` starts the checks without the ledger's credentials (`BENCHBOOK_DATABASE_URL`, `BENCHBOOK_TOKEN`,
`BENCHBOOK_READER_PASSWORD`, their `ASSAY_` forms, `PGPASSWORD`, `PGPASSFILE`) and without the GitHub token, so
code that only reads its own environment finds none of them. But the checks run as the same user as the action: a
process can still read its parent's environment (`/proc/<pid>/environ` on Linux) and files the user can read. So
the guarantee is against agents that misreport their work, not against a pull request written to steal the CI
credential. That is one more reason to keep the default `decide: false` and to give
CI a harness token rather than a lead's: a stolen harness token can add attempts, not decide.
