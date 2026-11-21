# Python SDK

```python
from benchbook import Ledger

Ledger.init_project("lr-sweep", goal="Minimise val loss", metric="val_loss", direction="min")  # pack="core" by default
lg = Ledger.open(project="lr-sweep", agent="worker-1")     # or BENCHBOOK_PROJECT / .benchbook/config.toml, BENCHBOOK_AGENT
```

Workers:

| call | returns |
|---|---|
| `next_task(tags=None, lease_seconds=900, spec_match=None)` | `Task` or `None`; skips tasks whose `spec.exclude_agents` lists you; `spec_match` keeps tasks whose spec contains that JSON |
| `heartbeat(task_id, lease_seconds=900)` | new expiry; raises `LeaseLost` |
| `brief(task_id=None, budget_tokens=2000)` | markdown |
| `check_tried(config, code_ref=None)` | `TriedResult(exact, replicates, best_value, dedup_key)` |
| `record_experiment(config, metrics, run_status="ok", seed=None, code_ref=None, notes=None, artifacts=None, task_id=None, trace_ref=None, duration_ms=None, tokens=None, cost_usd=None)` | `RecordResult(id, outcome, is_new_best, duplicate_of)` |
| `propose_claim(kind, statement, payload=None, tags=None, task_id=None, evidence=[(id, "supports")])` | `ClaimResult(id, duplicate_of)` |
| `link(src, relation, dst, note=None)` | link id |
| `add_source(uri, title=None, locator=None, sha256=None)` | source id |
| `complete_task(task_id, status="done", summary="")` | `Task`; raises `LeaseLost` |
| `create_intent(title, spec=None, tags=None, lease_seconds=900, exclusive=())` | a task already claimed by you, tagged `intent` (needs `settings.worker_intents` for workers); `Conflict` when a live intent has the same value for an `exclusive` spec field |
| `update_intent(task_id, spec_patch, lease_seconds=None, exclusive=())`, `my_intents()` | your live intents |
| `put_blob(data, media_type)`, `get_blob(uri)` | `benchbook://blob/<sha256>`; bytes |
| `baseline(partition=None)` | `(value, epsilon)` that the next outcome is compared with |
| `get(id)`, `search(text, kind, status, tags, limit, all_tags)` (`tags`: any of them, `all_tags`: every one), `status()`, `export()` | reads |

Orchestrator / human only (`PermissionDenied` otherwise): `create_tasks([...])`, `cancel_task(id, reason)`,
`update_settings(patch)` (JSON merge patch, audited in `settings_events`),
`decide(target, new_status, oracle, rationale, evidence_ids, oracle_detail=None, force=False)`.
`force` allows a transition outside the claim graph. The oracle and evidence requirements still apply.
`sql(query)` (read-only, `v_*` views) is open to every role in the SDK; the MCP server offers it in the orchestrator
tool set only.

A brief may shorten the displayed goal and task details to stay within the requested budget. A budget too small
for the required brief structure raises `ValidationFailed`.

Ids: every call accepts a short id (`e:91c0a2f1`) or a full UUID. Errors: `ValidationFailed` (with `.problems`, one
line per bad field), `NotFound`, `Ambiguous`, `PermissionDenied`, `LeaseLost`, `InvalidTransition`, `Conflict`, all subclasses of
`BenchbookError`.

A full loop is in `examples/sdk-loop/loop.py`.
