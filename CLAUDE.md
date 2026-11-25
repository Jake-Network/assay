# Working on benchbook

benchbook keeps a Postgres ledger beside a repository for coding agents. Its code mode records claims ("done", "tests
pass") and verifies them by an independent re-run (`src/benchbook/packs/code`); its experiment mode records experiments
and claims for agent swarms and hands the lead a budgeted brief (`src/benchbook/packs/core`,
`src/benchbook/packs/ml-research`). README.md explains the product, DECISIONS.md why things are the way they are, CHANGELOG.md what changed.

## Checks

- Run the tests for the files you touched: `uv run pytest -q tests/test_<area>.py`. The full suite takes over 20
  minutes; run it once at the end of a piece of work, not after every edit. One throwaway Postgres serves the whole
  session and each test gets a fresh project in it, so rows other tests wrote are still in the database.
- Before a commit: `uv run ruff check src tests bench examples scripts`, `uv run mypy src/benchbook`,
  `uv run pytest -q tests/test_docs.py`.
- A timing- or order-sensitive test that fails once in a full run: re-run it alone before calling it a regression.

## Code that is easy to misread

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
- A behavior change gets one line in CHANGELOG.md under the topmost `## Unreleased: <topic>` heading; a changed rule
  or default also gets an entry in DECISIONS.md.
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
