## benchbook research ledger

This repository records experiments in a benchbook ledger (`.benchbook/config.toml`). The ledger is the source of
truth for results; messages carry only short summaries and record ids (`e:`, `c:`, `t:`).

If a `benchbook` MCP server is configured, use its tools; otherwise use the CLI with `--json`.
Always identify yourself: MCP `agent` argument, or `BENCHBOOK_AGENT=worker-<n>` for the CLI.

1. Start: take a task (`benchbook_next_task` / `benchbook task next`), then read the brief (`benchbook_brief` /
   `benchbook brief --task <id>`).
2. With a shell, run each evaluation as one command that checks, runs and records it:
   `benchbook exp run --config '<json>' --seed <n> -- <evaluation command>`. `benchbook: not run: duplicate of ...`
   (exit 2) means it already ran: choose a different configuration. Otherwise its last line names the record;
   failures, errors and timeouts are recorded too.
3. Without a shell (MCP only): check before any evaluation that it was not already run (`benchbook_check_tried`)
   and record it right after (`benchbook_record_experiment`), including failures, errors and timeouts, with the
   cause in one sentence in notes.
4. Generalisable findings: propose a claim with its evidence ids (`benchbook_propose_claim` / `benchbook claim add`).
5. Finish: complete the task (`benchbook_complete_task` / `benchbook task done <id> -m "..."`). Your final message is a
   short summary with the record ids; the lead reads the details from the ledger.
6. Do not decide claim status; that is the orchestrator's (or a human's) job.

MCP server config (Codex `~/.codex/config.toml`; other MCP clients take the same command):

```toml
[mcp_servers.benchbook]
command = "benchbook"
args = ["mcp", "--toolset", "worker"]
```
