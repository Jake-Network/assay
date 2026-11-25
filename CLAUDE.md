# Working on benchbook

benchbook records coding-agent claims ("done", "tests pass") in a Postgres ledger and verifies them by independent
re-run. README.md explains the product, DECISIONS.md why things are the way they are, CHANGELOG.md what changed.

## Checks

- Run the tests for the files you touched: `uv run pytest -q tests/test_<area>.py`. The full suite takes over 20 minutes
  (each test gets a throwaway Postgres); run it once at the end of a piece of work, not after every edit.
- Before a commit: `uv run ruff check src tests bench examples scripts`, `uv run mypy src/benchbook`,
  `uv run pytest -q tests/test_docs.py`.
- A timing- or order-sensitive test that fails once in a full run: re-run it alone before calling it a regression.

## Changes

- One commit per item. Stage explicit paths, never `git add -A` or `git add .`.
- A behavior change gets one line in CHANGELOG.md under `## Unreleased`; a changed rule or default also gets an entry
  in DECISIONS.md.
- Keep pull requests small and stack them (base = the previous branch) rather than growing one large PR.
- Before pushing a non-trivial change, run `/code-review high` on it.

## Review findings

- Reproduce a finding first, and keep the reviewer's reproduction as a regression test.
- If a finding of the same kind comes back after a fix (a stale file accepted, a lost queue entry, a race between two
  writers), stop patching the symptom: change the design so the case cannot occur (a unique path per run, a stored
  decision, one lock for every writer), and say in DECISIONS.md why the first fix was not enough.

## Measurements

- `bench/results/<date>-*` are published results; their files (especially `tasks/`) have sha256 sums in the
  pre-registrations. Never edit them or recompute their numbers.
- `bench/*/PREREGISTRATION.md` bodies are frozen. Changes for a later run go in a dated Amendment, written only after
  the owner has decided to run it.
- Calls that cost money need the owner's approval in the current session: `claude -p`, `python -m bench.run --execute`,
  `python -m bench.boundary.run --execute`, `scripts/qa_boundary_hooks.sh`.
- Before a new feature is hardened or reviewed at length, check it works for its purpose with the smallest paid pilot
  (one task, one run per arm, a few USD). Earlier pilots of USD 1-3 found more than several review rounds did.
- Before a paid sweep: pin the Claude Code version (`DISABLE_AUTOUPDATER=1`, and record `claude --version` in the
  run), check the model and worker count each run actually used, check the account's five-hour usage left, and do one
  smoke run per configuration before the full matrix.
- Run long sweeps as a detached runner that writes its state to disk and resumes after a stop, not by watching them
  from a chat session.
