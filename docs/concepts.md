# Concepts

benchbook is a Postgres ledger for agent swarms. Workers write what they did (experiments, check runs, completions,
findings) to the ledger the moment it ends and hand back record ids. The lead reads the ledger, not the workers'
messages: a brief within a token budget, one record by id, anything else by read-only SQL. So the lead's context
holds conclusions and ids, and a swarm can grow without filling it. benchbook does not pass messages between agents
and it does not run them.

## How the ledger works

```
 worker-1 ─┐  report, check run, record_experiment (+ claims with evidence)
 worker-2 ─┼──────────────────────────────►  Postgres ledger  (append-only)
 worker-3 ─┘                                       │  SQL: re-runs, dedup, replicates, failures
                                                   ▼
 lead  ◄── brief (budgeted: verified, only reported, discarded, open findings) + ids
   └──► decide: supported / refuted / verified (non-LLM oracle + evidence only)
```

- **Nothing lost.** Every check run and every evaluation is a row the moment it ends. A hook captures any subagent
  output that cites no record, so even an agent that ignores the rules leaves a note.
- **Nothing repeated.** A failed approach is listed in the brief with its failure signature. For experiments, the
  same config + code + seed is refused, and a different seed counts as a replicate.
- **Checked before it is built on.** Agents propose claims. The lead, a hook or a person verifies them with a
  non-LLM oracle and evidence ids: a clean re-run for code, replicated seeds that still beat the baseline for
  experiments.

Triggers keep the records append-only, so an agent "cleaning up test data" cannot rewrite history. That protects
against mistakes, not against a hostile agent holding the owner's URL: see [Honest limits](#honest-limits).

Without Claude Code, the CLI and the [Python SDK](sdk.md) do the same:

```bash
export BENCHBOOK_AGENT=worker-1                                                     # who is writing
benchbook exp tried  --config '{"lr": 3e-4, "warmup": 500}'                         # before running
benchbook exp record --config '{"lr": 3e-4, "warmup": 500}' --metrics '{"val_loss": 2.91}' --seed 0
benchbook brief
```

## Two modes, one ledger

A project's pack decides which mode it is in. Both write the same records (below).

**Experiment swarms** (packs `core` and `ml-research`; `benchbook init --metric ...`). An attempt is an experiment:
a config, a code_ref and a seed, with its metrics. A duplicate config, code and seed is refused, another seed is a
replicate, and a result is `verified` once replicated runs by other agents still beat the baseline
([Claim lifecycle](#claim-lifecycle)). [autoresearch](autoresearch.md) runs on the `ml-research` pack.

**Coding swarms** (pack `code`; `benchbook init` without `--metric`). The work is split into pieces, and an
attempt is a commit of a piece judged by the declared checks ([code.md](code.md)):

| in coding work | record |
|---|---|
| a piece | a task: a worker intent tagged `intent`, `piece` and `piece:<name>`, whose spec holds the paths it occupies |
| a check run (`check verify`, `check run`) | an experiment: config `{"piece", "checks"}`, code_ref the commit, metrics `checks_passed` and `checks_total`; the piece is the partition |
| a re-run of a commit already checked | an experiment too: the code pack's `dedup_mode` is `notify`, so it is stored and names the first run |
| a worker's `DONE <sha>` | a `completion` claim `<piece> is complete at <sha12>`, tagged `piece:` and `commit:`, in the implementer's name (under a token, in the verifier's name with the implementer in `payload.reported_by`) |
| verifying it | a decision `verified`, oracle `rerun`, under the pack's `all_pass` rule |
| the checks pinned for a project or piece | a `note` claim tagged `checks-pin` |
| a discarded approach | an `observation` claim tagged `discarded` and `piece:<piece>`, with the failure signature |
| an out-of-scope finding | an `observation` claim tagged `out-of-scope` |
| a subagent report that cites no record | a `note` claim tagged `auto-captured` |
| the verifier | the agent `rerun@<host>` (orchestrator); the hooks write as `hook` (harness) |

## Records

| record | meaning | written by |
|---|---|---|
| project | a goal, a primary metric and its direction (code: `checks_passed`, per piece), a pack | a human or the lead (`benchbook init`) |
| agent | a contributor: worker, orchestrator, human or harness | registered on first use |
| task | a unit of work, held by one agent at a time through a lease | lead creates, worker claims |
| experiment | what was tried (config, code_ref, seed) and what happened (metrics, outcome, notes), failures included | workers, scripts |
| claim | a statement that can be supported or refuted; the pack sets its kinds: `hypothesis`, `observation`, `conclusion`, `note` (`core`), plus `result` and `calibration` (`ml-research`); `completion`, `observation`, `note` (`code`) | anyone proposes |
| link | a relation between records: supports, refutes, derived_from, depends_on, supersedes, replicates, cites | anyone |
| decision | a status change of a claim, or the invalidation of an experiment, with oracle and evidence | lead or human only |
| source | external evidence: a URL, file or paper page | anyone |

A claim's status is never stored: it is the latest decision, or `proposed` if there is none.

## Claim lifecycle

```
proposed ──► supported ──► verified ──► superseded / retracted
   │            │
   ├──► refuted ├──► refuted
   ├──► inconclusive (──► supported / refuted)
   └──► superseded / retracted
```

`verified` requires an oracle that is not an LLM (`metric`, `rerun`, `pytest`, `lean`, `human`) and at least one
evidence id. The `rerun` oracle compares successful runs of the original configuration and code. It needs
at least three distinct seeds and a rerun by a different agent. Projects can configure the minimum number of
seeds and authors (`rerun_min_seeds`, `rerun_min_authors`). For an improvement claim, both the mean of all
evidence and the mean of runs by agents other than the originating author must beat the baseline by epsilon.
The baseline comes from numeric `payload.baseline` or the best earlier result of another config in the same
partition. A claim that states a reproduced value sets `payload.measurement: true` and supplies a numeric
`payload.mean` that matches the evidence mean. Result claims also have to match the evidence mean.
Decisions store the values checked in `oracle_detail.rerun_check`. `llm_judge` can support a claim.

This is the default `rerun` rule (`[verify] rule = "replication"` in pack.toml). The code pack declares
`rule = "all_pass"` instead: a completion is verified by passing, clean re-runs of its piece at its commit by
agents other than its implementer, and a commit that weakened a test or changed `[checks]` needs a person; one that
only adds tests does not ([code.md](code.md#verified-completion)).

An experiment's only status change is `invalidated` (an evaluator bug or exploit was found). Invalidated
experiments are excluded from best, outcome calculation, "already tried", and recent brief activity. Claims
that rely entirely on invalidated evidence are flagged `evidence_invalidated`. A lead or human decides whether
to change the claim status.

## Outcomes

The server computes each experiment's outcome when it is recorded, against the current best (excluding
invalidated runs): `improved` (beyond `improve_epsilon`), `no_change`, `worse`, `failed` (run_status not ok) or
`unscored` (no primary metric). Authors cannot set it.

## Duplicates and replicates

`dedup_key = sha256(canonical config without ignored keys + code_ref)`. The same key with the same seed and an ok
run is refused and points to the original (`duplicate_of`). The same key with another seed is a replicate: it is
stored and aggregated in `v_tried`. Failed runs can be retried. `check_tried` before running counts as a
"repeat-check match" when it finds something; it also reports invalidated runs of the same key, which still
block re-recording the same seed (change `code_ref` after fixing an evaluator). `v_tried.summary_outcome` of a
config with two or more seeds compares its mean with the best mean of the other configs in its partition, so
one lucky seed does not keep it labelled `improved`; briefs mark a single-seed improvement as unreplicated.
Ignored keys may be nested: `optimizer.log_every`. Seed `NULL` and seed `-1` are different runs.

Whether an exact duplicate is refused is the pack's choice: `dedup_mode` in its `pack.toml`.

| `dedup_mode` | an ok run with a key and seed that already have an ok run | `record_experiment` returns | `dedup_hits` |
|---|---|---|---|
| `"refuse"` (default; `core`, `ml-research`) | not stored; the CLI exits 2 | `id` null, `duplicate_of` the earlier run | `rejected` |
| `"notify"` | stored, with `experiments.duplicate_of` set | a new `id`, `duplicate_of` the first run, and a `notice` sentence | `notified` |

Notify is for packs where running the same thing again is the point, such as a verifier re-running the checks of
the commit an implementer reported. Every repeat names the first run of its key and seed, not the previous repeat.
A repeat is an ordinary valid run: it counts in `v_tried` (`attempts` and `ok_runs`; `seeds` does not grow), can be
the best of its partition, and never becomes a second `v_partition_best` candidate. In notify mode a repeat of an
invalidated run is stored too. The CLI prints the notice on stderr (in the JSON with `--json`) and exits 0. The
database keeps refusing a second ok row for a key and seed unless the row declares the original it repeats.

Claims are deduplicated by their normalised statement; a duplicate's evidence is attached to the existing claim,
so independent agents reaching the same conclusion add up.

## Briefs

A brief is a deterministic markdown digest within a token budget (default 2,000): goal and best, your task,
verified claims, supported-but-unverified claims, what was already tried near your task (with failures first),
findings other agents proposed under your task's tags that nobody has judged yet (observations, notes),
open questions, and what changed since your last brief. Every item carries an id; details are one `benchbook_get`
away. No LLM is involved.

## Principles

1. **Append-only.** Knowledge records are never updated or deleted; a trigger refuses it. Changes are new rows
   linked with `supersedes`.
2. **One writer for decisions.** Workers add evidence; only the orchestrator or a human changes status.
3. **Validated on write.** Configs, metrics and claim payloads are checked against JSON Schemas; errors list each
   bad path so an agent can fix and resend.
4. **Only oracles verify.** See above.
5. **Failures are records, not context.** Negative results are always stored but shown only where relevant.
6. **Conclusions and ids in context.** Every read is budgeted; details on request.
7. **Zero setup.** Embedded Postgres locally; a URL for teams.
8. **Not an orchestrator.** benchbook records; your runtime runs agents.
9. **Not an observability tool.** Traces stay where they are; experiments keep a `trace_ref`.
10. **One place for logic.** CLI, MCP server, viewer and hooks are thin layers over the Python SDK.

## Honest limits

By default identity is self-declared. An MCP caller names itself with the `agent` argument and a CLI caller with
`BENCHBOOK_AGENT`/`--agent`, and an unknown name is registered with the role it asks for, so a malicious agent
could claim to be the lead, or make up several names to look like independent replicators. A CLI write without a
name is refused; a person deciding by hand passes `--as human`.

What the database enforces for every connection: append-only tables (UPDATE, DELETE and TRUNCATE are refused by
triggers), fixed agent roles (an agent row cannot be re-roled or deleted), and the decision-role check. The owner can
disable triggers (`ALTER TABLE ... DISABLE TRIGGER`), so while agents connect as the owner, as every agent on a
shared owner URL does, these hold against mistakes, not against an agent that sets out to break them.

A team can bind every write to its agent: machines get the `benchbook_writer` login and each agent a token, and the
database accepts a row only under the token's agent and lead-only actions only from an orchestrator or human token.
A token holder can still record false results under its own name. Setup and threat model:
[Several machines](multi-machine.md#per-agent-tokens).

`benchbook_sql` reads only the per-project views, as a read-only role ([schema.md](schema.md#views)). There is no
row-level security: a writer reads every project of its database.
