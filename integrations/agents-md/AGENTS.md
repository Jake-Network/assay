## benchbook research ledger

This repository records experiments in an benchbook ledger (`.benchbook/config.toml`). The ledger is the source of
truth for results; messages carry only short summaries and record ids (`e:`, `c:`, `t:`).

If an `benchbook` MCP server is configured, use its tools; otherwise use the CLI with `--json`.
Always identify yourself: MCP `agent` argument, or `BENCHBOOK_AGENT=worker-<n>` for the CLI.

1. Start: take a task (`benchbook_next_task` / `benchbook task next`), then read the brief (`benchbook_brief` /
   `benchbook brief --task <id>`).
2. Before any evaluation: check it was not already run (`benchbook_check_tried` / `benchbook exp tried --config ...`).
   If it was, choose a different configuration.
3. Right after any evaluation: record it (`benchbook_record_experiment` / `benchbook exp record ...`), including
   failures, errors and timeouts, with the cause in one sentence in notes.
4. Generalisable findings: propose a claim with its evidence ids (`benchbook_propose_claim` / `benchbook claim add`).
5. Finish: complete the task (`benchbook_complete_task` / `benchbook task done <id> -m "..."`). Your final message is at
   most 3 sentences plus record ids.
6. Do not decide claim status; that is the orchestrator's (or a human's) job.

MCP server config (Codex `~/.codex/config.toml`; other MCP clients take the same command):

```toml
[mcp_servers.benchbook]
command = "benchbook"
args = ["mcp", "--toolset", "worker"]
```
