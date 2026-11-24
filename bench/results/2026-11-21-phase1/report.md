# Phase 1 report: the two numbers (status on 2026-11-21)

**The coding number was measured on 2026-11-23 (17.9%, 7 of 39); the research number has not been measured yet.** Everything needed to measure them is built and passes its dry run; the
runs themselves spend API budget, Docker disk or GPU time and wait for the owner's go-ahead. This page is the one
place the two numbers, how they are measured and the cost analysis are kept; the empty cells are filled from the
commands below, and the verdict is then written to DECISIONS.md.

## The two numbers

| number | definition | source | value |
|---|---|---|---|
| false completion rate (coding) | completions with at least one refuting re-run / completions with at least one finished re-run of the declared checks | `benchbook sql` with the statement in docs/code.md, "The numbers"; SWE-bench Verified batch (`bench/code/`) plus field delegate runs | **17.9%** (7 of 39 completions re-run; 95% CI 9.0% to 32.7%), SWE-bench Verified, claude-sonnet-5-5, 2026-11-23: [../2026-11-23-swe-verified/](../2026-11-23-swe-verified/README.md) |
| keeps that did not reproduce (research) | keeps of an independent autoresearch run whose value did not hold when re-run by others / all its keeps | `bench/autoresearch/report.py` after `post_replicate.py`, per `bench/autoresearch/PREREGISTRATION.md` | not measured (0 of the 30 keeps needed) |

Beside the first number the SWE-bench report also gives the failure rate by the hidden tests (for comparison with
published numbers) and the number of completions that changed tests.

## How each is measured

**Coding, public task set.** `bench/code/PREREGISTRATION.md` fixes 40 instances of SWE-bench Verified
(`instances.json`: "<15 min fix", pure-Python repositories), one implementer session per instance, the visible and
hidden checks, the stopping rules. The bracketed values in it (model, budget caps, date) are proposals for the owner
to confirm before the run.

```bash
uv run python -m bench.code.run --dry                                                    # no model, no Docker
uv run python -m bench.code.run --out ~/benchbook-swe/run-1 --preflight --execute        # Docker pulls only (about 12 GB download, 50 GB free disk advised)
uv run python -m bench.code.run --out ~/benchbook-swe/run-1 --model claude-sonnet-5-5 \
    --max-budget-usd 3 --total-budget-usd 120 --wall-clock 40 --execute                  # estimated USD 20-80
uv run python -m bench.code.report ~/benchbook-swe/run-1
```

The Docker evaluation path has not run against a real image yet; the preflight is the step that tests it.

**Coding, field delegate runs.** In the owner's repository the lead adds one line to its integration step,
`benchbook check verify <piece> <commit> --reported-by <implementer>`, with `[checks]` declared in
`.benchbook/config.toml` (docs/code.md). Implementer instructions do not change. The same SQL statement gives the
rate.

**Research.** `bench/autoresearch/PREREGISTRATION.md` still has its [brackets] (GPU model and count, hours, budget,
Claude Code version, upstream commit, benchbook commit): only the owner can fill them. Then
`python -m bench.autoresearch.launch ... --execute` three times, `report.py`, `post_replicate.py`, and the README
table. The pipeline check on the CPU simulator passes on this branch (see "Checks run on this branch").

## Cost: where configuration C's tokens go

From the raw streams of the circle-packing batches (`../2026-11-20-claude-sonnet-5-5/cost-breakdown.md` and
`../2026-11-21-claude-sonnet-5-5-rerun/`, whose README explains why its C runs were repeated with the account's
connectors switched off). The baseline is the 2026-11-21 batch.

| mean per run, baseline | A text | B files | C benchbook |
|---|---|---|---|
| session tokens | 356,554 | 1,137,757 | 1,499,439 |
| API cost (USD) | 0.635 | 0.960 | 1.132 |
| lead context max (tokens) | 30,499 | 35,577 | 33,058 |
| duplicate ratio | 29.4% | 16.1% | 22.5% |
| lost results | 0 | 0.33 | 0 |

| hypothesis | verdict | number |
|---|---|---|
| 1. every attempt costs a separate lookup and a separate record call | partly supported in both batches | calls after `check_tried`/`record_experiment`: 29.8% and 30.3% of C's session-token gap over A; calls after any benchbook tool: 77.3% and 77.9% |
| 2. the exact hash misses near configurations | contradicted in both | near configs per run: C 0.7 and 1.3, B 6.0 and 6.3; C's repeats are replicates with a new seed |
| 3. the brief grows the lead's context | contradicted in the first batch (12.2% of the gap), 29.8% of a 2,559-token gap in the baseline | the brief is 762 tokens of C's largest lead prompt |

- The plan put C at about 5 times A's tokens. With the leak removed it is 4.2 times in session tokens and 1.8 times
  in cost; the first batch's 5.1 to 5.3 times included 8 connector tools in C's lead, and an account with more
  connectors (90 tools) drove it to 8.9 times.
- The gap is made of calls: C makes 111.7 model calls per run against A's 36.0, and each re-reads its agent's
  context. The first prompts, read again on every call, are 64.5% of C's prompt tokens; the task tools 8.5%, the
  brief 6.3%, `check_tried` 5.7%, `record_experiment` 4.3%.
- What follows for phase 2: `benchbook exp run` (one command per attempt) addresses hypothesis 1, which explains
  about 30% of the gap; a near-config notice (hypothesis 2) is not supported by the data; fewer task-tool round
  trips and a smaller first prompt for workers are where the rest is. C against A at +20% cost is far from the
  measured +78%.

## Verdict

| side | criterion | consequence | result |
|---|---|---|---|
| coding | false completion rate >= 10% | P0 and P1 go ahead | **met** (17.9%, 7 of 39; the CI 9.0% to 32.7% contains 10%) |
| coding | false completion rate < 10% | `check verify` and the PR evidence summary only | not met |
| research | keeps not reproduced >= 30% | headline, skill adapters | pending |
| research | keeps not reproduced 10-30% | compare with the stronger side | pending |
| research | keeps not reproduced < 10% | the reproduction gate becomes optional | pending |

If both numbers are under 10% and there is no way to bring C's cost within +20% of A, the plan stops. The thresholds
are drafts until they are fixed in the two pre-registration documents (open question 3).

On 2026-11-21 the owner asked for the phase 2 pieces of both sides to be built before the measurements. They are on
the `benchbook` branch; the verdict decides which of them are kept in the release and published, not whether they
get written.

## Checks run on this branch

- `uv run pytest`, `ruff`, `mypy --strict`: see the last commit's run in the branch notes.
- Rename bootstrap on a copy of the machine's own ledger (schema revision 0007; 7 projects, 233 experiments,
  49 claims): renamed and migrated to head, row counts unchanged, `status`, `brief` and `sql` work.
- autoresearch pipeline on the CPU simulator, after the rename: `examples/autoresearch-sim/run_sim_swarm.py --agents 3
  --minutes 1 --sleep 0.2` (no violations) and `python -m bench.autoresearch.launch --sim --agents 2 --minutes 1`
  (both arms, `post_replicate` and `report`) exit 0. They are pipeline checks, not evidence.
- Wheel smoke check (`scripts/smoke_wheel.sh`): `uvx --from dist/*.whl benchbook doctor`, `init`, `doctor` pass on
  Linux. macOS runs in CI and has not run yet (nothing is pushed).
