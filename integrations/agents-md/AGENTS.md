## assay research ledger

This repository records experiments in an assay ledger (`.assay/config.toml`). The ledger is the source of
truth for results; messages carry only short summaries and record ids (`e:`, `c:`, `t:`).

If an `assay` MCP server is configured, use its tools; otherwise use the CLI with `--json`.
Always identify yourself: MCP `agent` argument, or `ASSAY_AGENT=worker-<n>` for the CLI.

1. Start: take a task (`assay_next_task` / `assay task next`), then read the brief (`assay_brief` /
   `assay brief --task <id>`).
2. Before any evaluation: check it was not already run (`assay_check_tried` / `assay exp tried --config ...`).
   If it was, choose a different configuration.
3. Right after any evaluation: record it (`assay_record_experiment` / `assay exp record ...`), including
   failures, errors and timeouts, with the cause in one sentence in notes.
4. Generalisable findings: propose a claim with its evidence ids (`assay_propose_claim` / `assay claim add`).
5. Finish: complete the task (`assay_complete_task` / `assay task done <id> -m "..."`). Your final message is at
   most 3 sentences plus record ids.
6. Do not decide claim status; that is the orchestrator's (or a human's) job.

MCP server config (Codex `~/.codex/config.toml`; other MCP clients take the same command):

```toml
[mcp_servers.assay]
command = "assay"
args = ["mcp", "--toolset", "worker"]
```
