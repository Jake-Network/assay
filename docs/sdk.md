# Python SDK

```python
from assay import Ledger

Ledger.init_project("lr-sweep", goal="Minimise val loss", metric="val_loss", direction="min")
lg = Ledger.open(project="lr-sweep", agent="worker-1")     # or ASSAY_PROJECT / .assay/config.toml, ASSAY_AGENT
```

Workers:

| call | returns |
|---|---|
| `next_task(tags=None, lease_seconds=900)` | `Task` or `None` |
| `heartbeat(task_id, lease_seconds=900)` | new expiry; raises `LeaseLost` |
| `brief(task_id=None, budget_tokens=2000)` | markdown |
| `check_tried(config, code_ref=None)` | `TriedResult(exact, replicates, best_value, dedup_key)` |
| `record_experiment(config, metrics, run_status="ok", seed=None, code_ref=None, notes=None, artifacts=None, task_id=None, trace_ref=None, duration_ms=None, tokens=None, cost_usd=None)` | `RecordResult(id, outcome, is_new_best, duplicate_of)` |
| `propose_claim(kind, statement, payload=None, tags=None, task_id=None, evidence=[(id, "supports")])` | `ClaimResult(id, duplicate_of)` |
| `link(src, relation, dst, note=None)` | link id |
| `add_source(uri, title=None, locator=None, sha256=None)` | source id |
| `complete_task(task_id, status="done", summary="")` | `Task`; raises `LeaseLost` |
| `get(id)`, `search(text, kind, status, tags, limit)`, `status()`, `export()` | reads |

Orchestrator / human only (`PermissionDenied` otherwise): `create_tasks([...])`, `cancel_task(id, reason)`,
`decide(target, new_status, oracle, rationale, evidence_ids, oracle_detail=None, force=False)`.
`force` allows a transition outside the claim graph. The oracle and evidence requirements still apply.
`sql(query)` (read-only, `v_*` views) is open to every role in the SDK; the MCP server offers it in the orchestrator
tool set only.

A brief may shorten the displayed goal and task details to stay within the requested budget. A budget too small
for the required brief structure raises `ValidationFailed`.

Ids: every call accepts a short id (`e:91c0a2f1`) or a full UUID. Errors: `ValidationFailed` (with `.problems`, one
line per bad field), `NotFound`, `Ambiguous`, `PermissionDenied`, `LeaseLost`, `InvalidTransition`, all subclasses of
`AssayError`.

A full loop is in `examples/sdk-loop/loop.py`.
