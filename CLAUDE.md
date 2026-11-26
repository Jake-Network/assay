# Working on benchbook

benchbook is a Postgres ledger for agent swarms: subagents write their results to it instead of the lead's context,
and the lead reads them back as a budgeted brief and by SQL (DECISIONS.md, "Positioning"). Its experiment mode records
experiments and claims (`src/benchbook/packs/core`, `src/benchbook/packs/ml-research`); its code mode records check
runs and completions and re-runs a reported "done" before it counts (`src/benchbook/packs/code`). README.md explains
the product, docs/ how each part works, DECISIONS.md why, CHANGELOG.md what changed.

## Checks

- Run the tests for the files you touched: `uv run pytest -q tests/test_<area>.py`. Run the full suite once at the
  end of a piece of work, in parallel (about 2 minutes on 12 workers, over 20 serially), then the 50 ms hook timing
  tests alone, since the other workers' load slows them: `uv run pytest -q -n auto -k "not within_50ms"` and
  `uv run pytest -q tests/test_hooks.py -k within_50ms`. Each test process (each xdist worker) has one throwaway
  Postgres and each test gets a fresh project in it, so rows other tests wrote are still in the database.
- Before a commit: `uv run ruff check src tests bench examples scripts`, `uv run mypy src/benchbook`,
  `uv run pytest -q tests/test_docs.py`.
- A timing- or order-sensitive test that fails once in a full run: re-run it alone before calling it a regression.

## Code that is easy to misread

- Layout: `core/` (ledger, models, brief, runner), `checks/` (declared checks, `check verify` by stage), `code/`
  (pieces, findings, PR evidence), `hooks/`, `cli/`; DECISIONS.md, "Package layout". Keep the `__init__.py` of
  `checks/` and `hooks/` empty (the 50 ms budget below), and call a function that tests replace through its
  module (`rerun.run_checks`, `spooler.start_processor`) so the replacement reaches every caller.
- Hooks run on every tool call of someone else's session. They never write to stderr, exit 0, and outside a
  benchbook project leave without touching the database. For a Bash call that is not a declared check,
  `hook bash-result` returns within 50 ms, imports included (`tests/test_hooks.py`), so keep heavy imports off that
  path. The one exception is the completion gate, `hook task-completed`, which exits 2 with a stderr message to keep
  a task open.
- The pre-rename names (`assay`, `ASSAY_*`, `.assay`, `assay-worker`, the `assay_*` MCP tools,
  `assay:auto-captured`) are kept on purpose so installs from before the rename keep working; they are not leftovers.
  Accept old names wherever they were persisted, write only new ones (DECISIONS.md, "Name").
- A schema change is a new Alembic revision: `src/benchbook/db/alembic/versions/NNNN_<name>.py` running one plain
  `src/benchbook/db/alembic/sql/NNNN_<name>.sql`. Migrations are forward-only; never edit a revision or SQL file that
  is on main.
- Files that change together: README.md and README.ko.md; the autoresearch template in
  `integrations/autoresearch/template/` and its packaged copy under `src/benchbook/integrations/autoresearch/`
  (`tests/test_docs.py` compares them). A hook that reads a new field of a harness's input quotes that harness's
  documentation for it in docs/harnesses.md.

## Changes

- One commit per item. Stage explicit paths, never `git add -A` or `git add .`.
- A behavior change gets one line in CHANGELOG.md under `## Unreleased`, in its area's `###` group. The section
  describes the release as it will ship: when a later change replaces an earlier line, rewrite that line.
- A changed rule or default updates its entry in DECISIONS.md, which is grouped by topic and states current
  decisions with their reasons: rewrite the entry in place rather than appending a dated section.
- docs/ and README describe current behaviour in the present tense. History ("before, it...", pilot and review
  stories) goes to the commit message, CHANGELOG.md or bench/results/, not into a reference doc.
- Keep pull requests small and stack them (base = the previous branch) rather than growing one large PR.
- Before pushing a non-trivial change, run `/code-review high` on it.

## Review findings

- Reproduce a finding first, and keep the reviewer's reproduction as a regression test.
- If a finding of the same kind comes back after a fix (a stale file accepted, a lost queue entry, a race between two
  writers), stop patching the symptom: change the design so the case cannot occur (a unique path per run, a stored
  decision, one lock for every writer), and say in DECISIONS.md why the first fix was not enough.

## Paid runs

- Calls that cost money (API or GPU time) need the owner's approval in the current session: `claude -p`,
  `scripts/qa_boundary_hooks.sh`, and every `--execute` under `bench/` (`bench.run`, `bench.boundary.run`,
  `bench.code.run`, `bench.autoresearch.launch`, `bench.autoresearch.post_replicate`).
- Before a new feature is hardened or reviewed at length, check it works for its purpose with the smallest paid pilot
  (one task, one run per arm, a few USD). Earlier pilots of USD 1-3 found more than several review rounds did.
  bench/CLAUDE.md has the rules for measurements.
