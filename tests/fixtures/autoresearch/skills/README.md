# Run records of the autoresearch skills

Samples for `tests/test_ar_skills.py`, one per adapter in `src/benchbook/integrations/autoresearch/skills.py`.
Each file is written by hand in the format the skill's source code writes, as read at the upstream commit below:
the field names, value types, status words, file names and layout are the upstream ones, the values are invented. Commit hashes are placeholders (`aaa1111`, `1111…`) that
the tests replace with real commits of a test checkout.

| skill | file | upstream commit | where the format is defined |
|---|---|---|---|
| [davebcn87/pi-autoresearch](https://github.com/davebcn87/pi-autoresearch) | `pi-autoresearch/log.jsonl` (`.auto/log.jsonl`) | `04b8a50dac7efe80ffc91648bd00ed9b022e24c2` (v1.9.0, 2026-10-07) | `extensions/pi-autoresearch/index.ts` (init_experiment writes the `{"type":"config"}` line, log_experiment appends `{run, commit, metric, metrics, status, description, timestamp, segment, confidence, asi?}`), `jsonl.ts` (how the extension reads it back), `paths.ts` (`.auto/log.jsonl`, legacy `autoresearch.jsonl`) |
| [uditgoenka/autoresearch](https://github.com/uditgoenka/autoresearch) | `uditgoenka/results.tsv` (`autoresearch/loop-<YYMMDD>-<HHMM>/*-results.tsv`) | `7bddd056dfd0b0ded8360588d8101b1ecb5c0baa` (v2.3.0, 2026-10-31) | `plugins/autoresearch/skills/autoresearch/autoresearch.md` (header, columns, the iteration 0 row, Phase 6 statuses, Phase 7 row), `docs/code-standards.md` (the eight status values), `evals.md` (`*-results.tsv`, v2.0.03 files without `timestamp`) |
| [leo-lilinxiao/codex-autoresearch](https://github.com/leo-lilinxiao/codex-autoresearch) | `codex-autoresearch/run.json`, `codex-autoresearch/events.jsonl` (`autoresearch-results/`) | `0089369d803a2f3fa2a804aaf854c194cbc00241` (2026-11-19) | `scripts/autoresearch_core.py` (`RUN_KEYS`, `EVENT_FIELDS`, `validate_event`, `derive_state`: schema 2, one baseline event, then iteration events with keep/discard/crash and the commit provenance rules) |

The uditgoenka TSV is written by the agent following the command's instructions, not by code, so a real file can
differ in small ways (a missing `timestamp` column in v2.0.03 files); the reader finds columns by name. The TSV
does not name its metric, only its direction.
