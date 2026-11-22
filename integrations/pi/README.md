# Benchbook hooks for Pi

Install `benchbook` on PATH and run `benchbook init` in the checkout. Copy [benchbook.ts](benchbook.ts) to
`.pi/extensions/benchbook.ts` and trust the project in Pi. Use `pi -e /path/to/benchbook.ts` to load the extension
for one session.

The extension loads the scoped brief at session start and adds it before the first model run. It appends pending
notes to tool results and observes declared checks when Pi's `bash` result includes a structured exit code. To
uninstall, remove the extension file.

[Hook fields and fixture provenance](../../docs/harnesses.md#pi)
