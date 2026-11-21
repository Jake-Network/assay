---
name: benchbook
description: Rules for using the benchbook research ledger (record every experiment, check before running, cite record ids). Use whenever a task involves running evaluations or experiments in a repository that has .benchbook/config.toml.
---

# Using the benchbook ledger

benchbook is this project's shared lab notebook. Results live in the ledger; messages carry only short
summaries and record ids (`e:` experiment, `c:` claim, `t:` task, `s:` source, `d:` decision).

- Every MCP call takes `agent`: your benchbook agent name (`lead` for the main session, `worker-<n>` for workers).
- Before an evaluation: `benchbook_check_tried`. After it: `benchbook_record_experiment`, failures included.
- Findings that generalise: `benchbook_propose_claim` with evidence ids. Identical statements are merged and the
  evidence is attached to the existing claim.
- Read `benchbook_brief` instead of re-reading transcripts; use `benchbook_get(<id>)` only for details.
- Only the orchestrator (`lead`) or a human writes decisions. `verified` needs a non-LLM oracle
  (metric, rerun, pytest, lean, human) and evidence; an LLM judgement can reach `supported` at most.
- Without MCP, the same operations exist in the CLI (set `BENCHBOOK_AGENT=<your name>` first; writes without a name
  are refused): `benchbook brief`, `benchbook exp tried --config ...`,
  `benchbook exp record ...`, `benchbook claim add ...` (all accept `--json`).
