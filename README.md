# benchbook: "done" is a claim until the tests say so

**Your coding agents report "done". benchbook re-runs the checks on that exact commit, flags tests weakened to get
green, and keeps what was verified across subagents, sessions and compaction.**

On SWE-bench Verified, 7 of 39 tasks Claude Code reported done failed the benchmark's tests when re-run, and only 3
of the 39 agents had run a test first ([results](docs/results.md)). benchbook keeps a Postgres ledger beside your
repository. A report becomes a claim; only an independent re-run of your declared checks, in a clean worktree, makes
it `verified`.

## Start (Claude Code)

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook   # no Docker needed
benchbook install --no-cli       # the hook plugin; --mcp adds the MCP server and the worker agent
cd your-repo && benchbook init   # proposes your test and lint commands as checks
```

Then work as usual. The one convention: an implementer ends its report with `DONE <sha>`, and the piece is named by
its branch, a `piece: <name>` line or a `Benchbook-Piece:` commit trailer.

## What you get

- **Every "done" is re-run.** A subagent's `DONE <sha>` starts the declared checks on that commit in a clean
  worktree, in the background. The next turn hears the outcome once:
  `benchbook: media-upload a1b2c3d: re-run passed (2 checks, 14.2s), tests not weakened`.
- **Weakened tests go to a person, however green.** Deleted or changed asserts, new `skip`/`xfail`, a
  `conftest.py` change, fewer tests run than at the merge-base.
- **"Passing" cannot be redefined.** Checks are declared once in `.benchbook/config.toml` and pinned in the ledger.
  An implementer's own runs are recorded but never count as the re-run.
- **Failures are remembered.** A failed re-run leaves a discarded approach with its failure signature, and the next
  agent is told not to repeat it.
- **A brief at session start and after compaction** (experimental): verified pieces, undecided re-runs, discarded
  approaches and occupied paths in at most 1,500 tokens.

Hooks never write to stderr and always exit 0; outside a benchbook project they do nothing.
[What happens when](docs/claude-code.md#what-happens-in-a-session) · [the verification policy](docs/code.md)

## Commands for the lead

```bash
benchbook --agent lead check verify media-upload a1b2c3d --reported-by implementer-1   # re-run and decide
benchbook brief --here            # the brief for what this branch changed
benchbook status                  # verified, undecided, reported only, mismatched; open findings; hook health
benchbook piece start media-upload --paths 'src/media/**'   # occupy paths so two implementers do not collide
```

The [GitHub Action](integrations/github-action/README.md) re-runs each piece a pull request completes and keeps one
comment saying which completions were confirmed. Codex, Cursor, Gemini CLI, OpenCode and Pi get the brief and the
check recording too ([harnesses](docs/harnesses.md)); the automatic re-run is Claude Code only.

## Measured so far

- **"Done" is often not done.** SWE-bench Verified with `claude-sonnet-5-5`: 7 of 39 reported completions failed the
  benchmark's checks on re-run (17.9%, 95% CI 9.0% to 32.7%). Re-running only the visible tests, as benchbook does in
  real use, caught 3 of them (in two the reference fix fails the same test).
- **Memory across restarts is not proven yet.** In four one-trial pilots, a lead reading the brief answered fewer
  state questions than one keeping progress files or one resuming its session. No pilot reached compaction.
- **Experiment swarms lose nothing.** Circle packing: no lost results and fewer duplicate evaluations than plain
  subagents (22.5% vs 29.4%, within one standard deviation), at 1.8x the API cost and a higher peak lead context.

Methods, every pilot and the raw reports: [docs/results.md](docs/results.md).

## Experiment swarms

benchbook started as a lab notebook for agents that run experiments. Workers record every run, failures included,
and check what was tried before running; the same config, code and seed is refused and a new seed counts as a
replicate. The lead reads a budgeted brief instead of transcripts.

```bash
cd your-repo && benchbook init --goal "Minimise validation loss" --metric val_loss --direction min
benchbook install --no-cli --mcp
```

Then `/benchbook-mcp:benchbook-lead Minimise val_loss of train.py; 3 rounds, 4 workers` in Claude Code, and
`benchbook serve` to watch at http://127.0.0.1:7466. On many GPUs, a result becomes the shared baseline only after
two other agents replicate it: [autoresearch](docs/autoresearch.md).

## What it fits

- **Coding work with a test suite**, split across agents or sessions, each piece ending at a commit the checks can
  judge.
- **Experiments** that are all three: **A number per attempt** (every attempt yields a metric), **Re-runnable** (the
  same config runs again with only the seed changed) and **One space, explored together** (several workers search
  it at once).

## What it is not

- Not a code reviewer or test writer: a green re-run says nothing about what your checks do not cover.
- Not a duplicate blocker for code implementation: for code it remembers which approaches failed, not exact repeats.
- Not an orchestrator, a single-agent memory (mem0, Letta) or an observability tool (records can carry a `trace_ref`).
- Not a defence against a hostile agent on a shared owner URL: the guarantees catch mistakes
  ([honest limits](docs/concepts.md#honest-limits)).

## Docs

[Coding work](docs/code.md) · [Claude Code](docs/claude-code.md) · [Other harnesses](docs/harnesses.md) ·
[Results](docs/results.md) · [Concepts](docs/concepts.md) · [Schema and SQL](docs/schema.md) ·
[Several machines](docs/multi-machine.md) · [Python SDK](docs/sdk.md) · [autoresearch](docs/autoresearch.md) ·
[Circle-packing demo](examples/circle-packing/README.md) · [Releasing](docs/releasing.md)

License: Apache-2.0.
