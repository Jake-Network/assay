# Schema

Postgres schema `benchbook` (migrations: Alembic revisions that run plain SQL files in
`src/benchbook/db/alembic/sql/`). Knowledge tables are append-only; triggers reject UPDATE and DELETE for every
role, superusers included.

## Tables

| table | what | mutable |
|---|---|---|
| `projects` | goal, primary metric and direction, improve epsilon, pack, settings (`config_schema`, `rerun_min_seeds`, `rerun_min_authors`, `partition_key`, `worker_intents`, `partitions`, ...) | settings only, through `update_settings` |
| `agents` | contributor identity per project, `role` in worker / orchestrator / human / harness | no |
| `tasks` | unit of work with a lease (`lease_owner`, `lease_expires_at`, `attempts`) | lease fields, through SDK functions only |
| `task_events` | audit log of task changes (created, claimed, heartbeat, expired, done, failed, cancelled) | no |
| `experiments` | config, code_ref, dedup_key, seed, run_status, metrics, primary_value, server-computed outcome, notes, artifacts, trace_ref | no |
| `claims` | statement (1-500 chars), kind, payload, statement_hash (unique per project) | no |
| `sources` | external evidence: uri, title, locator, sha256 | no |
| `links` | src -relation-> dst between claims, experiments, sources, tasks | no |
| `decisions` | status transitions of claims, or `invalidated` for experiments; oracle, rationale, evidence_ids | no |
| `brief_log` | one row per brief (for "since your last brief") | no |
| `dedup_hits` | `rejected` (a duplicate record was refused) and `avoided` (a repeat check matched) | no |
| `settings_events` | every settings change: agent, JSON merge patch, resulting settings | no |
| `blobs` | small files by content (`benchbook://blob/<sha256>`), per project; 1 MB default limit (`blob_max_bytes`) | no |

Partitions (Step 3): when `settings.partition_key` is set, outcomes, `v_best`, `v_leaderboard`, `v_progress`
and duplicate checks only compare experiments whose `config[partition_key]` is equal. Projects without it behave as
before.

## Views

`benchbook_sql` (and `benchbook sql`) can read only these views, through the `benchbook_reader` role, already
filtered to the current project, read-only, 2 s timeout, 200 rows / ~4,000 tokens.

| view | columns (main) |
|---|---|
| `v_claim_status` | id, kind, statement, tags, author, status, last_oracle, decided_at, n_support, n_refute, n_support_authors, evidence_invalidated |
| `v_best` | the best valid ok experiment (one per partition when `partition_key` is set): id, author, config, metrics, primary_value, outcome, notes, partition |
| `v_leaderboard` | top 50 valid experiments with `rank` (per partition), partition |
| `v_tried` | per dedup_key: attempts, seeds, ok_runs, mean_value, std_value, best_value, n_improved, n_no_change, n_worse, n_failed, n_unscored, authors, summary_outcome, config, code_ref, last_id, last_notes |
| `v_negative` | dedup_keys that never improved: config, attempts, seeds, n_no_change, n_worse, n_failed, notes |
| `v_open_tasks` | claimable tasks: id, title, tags, priority, spec, attempts, expired_owner |
| `v_progress` | best-so-far curve (per partition): id, author, created_at, primary_value, best_so_far, n, tokens_cum, partition, seed |
| `v_partition_best` | per partition: `verified` result claims and `candidate` first runs awaiting judgement, with mean, std, n, code_ref, author, origin_id, rank |
| `v_lineage` | experiment, parent_id (derived_from), depth, root_id, changed, description, outcome, primary_value, partition |
| `v_inflight` | claimed tasks with a live lease: agent, title, spec, is_intent, partition, lease_left_s |
| `v_knob_history` | per (partition, knob, value) over first runs: n, n_improved, n_no_change, n_worse, n_failed, n_verified, n_refuted, n_inconclusive, last_at |
| `v_agent_stats` | agent, role, experiments, improved, claims, dedup_avoided, dedup_rejected, last_active_at |
| `v_dedup_savings` | rejected, avoided |
| `v_activity` | kind, id, agent, summary, created_at across all record kinds |

Invalidated experiments are excluded from every experiment-based view, from best and outcome computation,
and from briefs.

## Example queries

These are tested against a populated ledger in `tests/test_docs.py`.

1. Hypotheses supported by at least two independent agents and refuted by none:

```sql
SELECT id, statement, n_support, n_support_authors FROM v_claim_status
WHERE kind = 'hypothesis' AND n_support_authors >= 2 AND n_refute = 0
```

2. The ten best configurations by mean over replicates (at least two seeds):

```sql
SELECT config, seeds, mean_value, std_value FROM v_tried
WHERE seeds >= 2 ORDER BY mean_value DESC LIMIT 10
```

3. Configurations that failed most often, with their last error note:

```sql
SELECT config, n_failed, last_notes FROM v_tried WHERE n_failed > 0 ORDER BY n_failed DESC LIMIT 10
```

4. Never-improving regions to stop exploring:

```sql
SELECT config ->> 'init' AS init, count(*) AS configs, sum(attempts) AS runs
FROM v_negative GROUP BY 1 ORDER BY runs DESC
```

5. Which agent found improvements, and how much each one repeated:

```sql
SELECT agent, experiments, improved, dedup_avoided, dedup_rejected FROM v_agent_stats
WHERE role = 'worker' ORDER BY improved DESC
```

6. Best-so-far over time (for a plot):

```sql
SELECT n, created_at, best_so_far FROM v_progress ORDER BY n
```

7. Claims whose only supporting evidence was invalidated (need a new decision):

```sql
SELECT id, statement, status FROM v_claim_status WHERE evidence_invalidated
```

8. Supported claims still waiting for verification, oldest first:

```sql
SELECT id, statement, n_support, decided_at FROM v_claim_status
WHERE status = 'supported' ORDER BY decided_at
```

9. Effect of one config key: mean best value per optimizer:

```sql
SELECT config ->> 'optimizer' AS optimizer, count(*) AS configs, avg(best_value) AS avg_best, max(best_value) AS top
FROM v_tried WHERE best_value IS NOT NULL GROUP BY 1 ORDER BY top DESC
```

10. What happened in the last 30 minutes, by kind:

```sql
SELECT kind, count(*) AS n FROM v_activity
WHERE created_at > now() - interval '30 minutes' GROUP BY kind ORDER BY n DESC
```
