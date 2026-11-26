# benchbook: a Postgres ledger for agent swarms

**Your subagents write their results to Postgres, not into the lead's context. The lead reads them back by SQL
instead of from the subagents' reports.**

A lead agent that fans work out to dozens of subagents gets every result back as natural-language text. With enough
workers those reports fill its context window, and what they said is lost at the next compaction or session.
benchbook keeps a ledger beside your repository: each worker records its experiments, check runs and completions the
moment they end and hands back only record ids, and the lead pulls what it needs from the ledger.

## Start

```bash
uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook   # no Docker needed
benchbook install --no-cli --mcp   # Claude Code plugins: the hooks, plus the MCP server and the worker agent
```

**A coding swarm.** `cd your-repo && benchbook init` proposes your test and lint commands as checks. Then work as
usual; the one convention is that an implementer ends its report with `DONE <sha>`.

**An experiment swarm.** `benchbook init --goal "Minimise validation loss" --metric val_loss --direction min`, then in
Claude Code `/benchbook-mcp:benchbook-lead Minimise val_loss of train.py; 3 rounds, 4 workers`, and `benchbook serve`
to watch at http://127.0.0.1:7466.

## What you get

- **Results in the ledger, ids in the context.** Workers record every run, failures included, and hand back a
  short summary with record ids. The lead reads a budgeted brief (best result, open work, what not to repeat) and
  queries details by SQL when it needs them.
- **Nothing lost.** Every evaluation a worker records, and every run of a declared check the hooks can read, is a
  row the moment it ends. A hook keeps a subagent's report even when the agent ignored the rules.
- **Nothing repeated.** The same config, code and seed is refused; a new seed counts as a replicate. A failed
  approach stays in the brief with its failure signature.
- **Across sessions and compaction** (experimental): a new or compacted session starts from the brief, not from
  what someone remembered.
- **Results are checked before they are built on.** An experiment result becomes the baseline only after other
  agents replicate it, and a worker's `DONE <sha>` is re-run on that commit in a clean worktree
  ([coding work](docs/code.md)).

Hooks never write to stderr and exit 0, except the completion gate, which exits 2 to keep a task open; outside a
benchbook project they do nothing.

## Commands for the lead

```bash
benchbook brief --here            # what the swarm has done on this branch, in a token budget
benchbook status                  # verified, undecided, reported only, mismatched; open findings; hook health
benchbook sql "SELECT ..."        # anything else, read-only
benchbook get c:7f3a91c2          # one record in at most 400 tokens
```

The [GitHub Action](integrations/github-action/README.md) re-runs each piece a pull request completes. Codex,
Cursor, Gemini CLI, OpenCode and Pi get the brief and the check recording ([harnesses](docs/harnesses.md)); a
Python SDK and the CLI work without any harness.

## Measured so far

- **The lead reads less than half the report tokens, but its peak context is not lower yet.** Circle packing at 8
  and 16 workers: 6.9k and 13.8k report tokens reached the lead with benchbook, against 15.4k and 31.1k with plain
  subagents; peak lead context was 55.2k against 47.2k and 85.9k against 83.5k. At 30 workers both benchbook runs
  ran under other conditions (Haiku workers, or 28 of 90 workers), so they are observations, not a comparison.
- **Swarms lose nothing.** No lost results and fewer duplicate evaluations than plain subagents (22.5% vs 29.4%,
  within one standard deviation), at 1.8x the API cost.
- **Memory across restarts is not proven.** In four one-trial pilots a lead reading the brief answered fewer state
  questions than one keeping progress files. No pilot reached compaction.
- **Workers' "done" needs checking.** On SWE-bench Verified, 7 of 39 completions Claude Code reported done failed the
  benchmark's tests on re-run (17.9%, 95% CI 9.0% to 32.7%).

Methods, every pilot and the raw reports: [docs/results.md](docs/results.md).

## What it fits

- **Coding work with a test suite**, split across many agents or sessions, each piece ending at a commit the checks
  can judge.
- **Experiments** that are all three: **A number per attempt** (every attempt yields a metric), **Re-runnable** (the
  same config runs again with only the seed changed) and **One space, explored together** (several workers search
  it at once). On many GPUs: [autoresearch](docs/autoresearch.md).

## What it is not

- Not an orchestrator: it does not run or route agents. It sits under Claude Code, Codex, Cursor or your own loop.
- Not a code reviewer or test writer: a re-run checks only what your declared checks cover.
- Not a duplicate blocker for code implementation: for code it remembers which approaches failed, not exact repeats.
- Not a single-agent memory (mem0, Letta) or an observability tool (records can carry a `trace_ref`).
- Not a defence against a hostile agent on a shared owner URL: the guarantees catch mistakes
  ([honest limits](docs/concepts.md#honest-limits)).

## Docs

[Concepts](docs/concepts.md) · [Coding work](docs/code.md) · [Claude Code](docs/claude-code.md) ·
[Other harnesses](docs/harnesses.md) · [Results](docs/results.md) · [Schema and SQL](docs/schema.md) ·
[Several machines](docs/multi-machine.md) · [Python SDK](docs/sdk.md) · [autoresearch](docs/autoresearch.md) ·
[Circle-packing demo](examples/circle-packing/README.md) · [Releasing](docs/releasing.md)

License: Apache-2.0.