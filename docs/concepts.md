# Concepts

assay is the record of a research community made of agents: workers are contributors, the lead is the editor,
the database is the journal. It does not pass messages between agents and it does not run them.

## Records

| record | meaning | written by |
|---|---|---|
| project | one research goal: goal sentence, primary metric and direction, pack | a human or the lead (`assay init`) |
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
seeds and authors. `llm_judge` can support a claim.

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
"repeat-check match" when it finds something.

Claims are deduplicated by their normalised statement; a duplicate's evidence is attached to the existing claim,
so independent agents reaching the same conclusion add up.

## Briefs

A brief is a deterministic markdown digest within a token budget (default 2,000): goal and best, your task,
verified claims, supported-but-unverified claims, what was already tried near your task (with failures first),
open questions, and what changed since your last brief. Every item carries an id; details are one `assay_get`
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
8. **Not an orchestrator.** assay records; your runtime runs agents.
9. **Not an observability tool.** Traces stay where they are; experiments keep a `trace_ref`.
10. **One place for logic.** CLI, MCP server, viewer and hooks are thin layers over the Python SDK.

## Honest limits

Roles are enforced cooperatively. An MCP caller names itself with the `agent` argument, so a malicious agent could
claim to be the lead. What holds regardless: the append-only and decision-role triggers in the database, and
Claude Code's tool allow-lists, which keep `assay_decide` out of the worker subagent's reach. Per-agent tokens and
row-level security for teams are planned.
