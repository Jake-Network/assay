---
name: assay
description: Rules for using the assay research ledger (record every experiment, check before running, cite record ids). Use whenever a task involves running evaluations or experiments in a repository that has .assay/config.toml.
---

# Using the assay ledger

assay is this project's shared lab notebook. Results live in the ledger; messages carry only short
summaries and record ids (`e:` experiment, `c:` claim, `t:` task, `s:` source, `d:` decision).

- Every MCP call takes `agent`: your assay agent name (`lead` for the main session, `worker-<n>` for workers).
- Before an evaluation: `assay_check_tried`. After it: `assay_record_experiment`, failures included.
- Findings that generalise: `assay_propose_claim` with evidence ids. Identical statements are merged and the
  evidence is attached to the existing claim.
- Read `assay_brief` instead of re-reading transcripts; use `assay_get(<id>)` only for details.
- Only the orchestrator (`lead`) or a human writes decisions. `verified` needs a non-LLM oracle
  (metric, rerun, pytest, lean, human) and evidence; an LLM judgement can reach `supported` at most.
- Without MCP, the same operations exist in the CLI: `assay brief`, `assay exp tried --config ...`,
  `assay exp record ...`, `assay claim add ...` (all accept `--json`).
