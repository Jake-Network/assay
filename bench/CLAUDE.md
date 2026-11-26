# Measurements in bench/

The root CLAUDE.md applies here too, including the approval rule for paid runs.

- `bench/results/<date>-*` are published results; their files (especially `tasks/`) have sha256 sums in the
  pre-registrations. Never edit them or recompute their numbers.
- `bench/*/PREREGISTRATION.md` bodies (autoresearch, boundary, code) are frozen. Changes for a later run go in a
  dated Amendment, written only after the owner has decided to run it.
- Before a paid sweep: pin the Claude Code version (`DISABLE_AUTOUPDATER=1`, and record `claude --version` in the
  run), check the model and worker count each run actually used, check the account's five-hour usage left, and do one
  smoke run per configuration before the full matrix.
- Run long sweeps as a detached runner that writes its state to disk and resumes after a stop, not by watching them
  from a chat session.
