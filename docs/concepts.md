# Concepts

benchbook is the record of a research community made of agents: workers are contributors, the lead is the editor,
the database is the journal. It does not pass messages between agents and it does not run them.

## Records

| record | meaning | written by |
|---|---|---|
| project | one research goal: goal sentence, primary metric and direction, pack | a human or the lead (`benchbook init`) |
| agent | a contributor: worker, orchestrator, human or harness | registered on first use |
| task | a unit of work, held by one agent at a time through a lease | lead creates, worker claims |
| experiment | what was tried (config, code_ref, seed) and what happened (metrics, outcome, notes), failures included | workers, scripts |
| claim | a statement that can be supported or refuted: hypothesis, observation, conclusion, note | anyone proposes |
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

Identity is self-declared. An MCP caller names itself with the `agent` argument and a CLI caller with
`BENCHBOOK_AGENT`/`--agent`, and an unknown name is registered with the role it asks for, so a malicious agent
could claim to be the lead, or make up several names to look like independent replicators. The CLI no longer
falls back to `human` when no name is given; a person deciding by hand passes `--as human`.

What the database enforces for every connection that is not the owner's: append-only tables (UPDATE, DELETE and
TRUNCATE are refused by triggers), fixed agent roles (an agent row cannot be re-roled or deleted), and the
decision-role check. The owner can disable triggers (`ALTER TABLE ... DISABLE TRIGGER`), and today every agent
connects as the owner, so these hold against mistakes, not against an agent that sets out to break them.

`benchbook_sql` runs as `benchbook_reader` in a read-only transaction over the per-project `benchbook_q` views. The reader
cannot call `set_config` (revoked when the migration runs as a superuser), the query text may not use
`set_config`, `U&` escapes, the `*_to_xml` functions or `pg_settings`, and a query that changes the project or
role is discarded. When `benchbook_reader` cannot log in, the weaker `SET ROLE` fallback needs
`BENCHBOOK_SQL_ALLOW_SET_ROLE=1`.

Planned for v0.2: per-agent tokens, a runtime writer role with INSERT and SELECT only (separate from the
migration owner), and row-level security per project.
