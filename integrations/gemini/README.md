# Benchbook hooks for Gemini CLI

Run `benchbook install --harness gemini` to merge the hooks into your user settings. To install only in one project,
merge the `hooks` object from [hooks.json](hooks.json) into that project's `.gemini/settings.json` and trust the
project in Gemini CLI. Install at one level so the same command runs once.

Run `benchbook init` in the project. `SessionStart` loads the scoped brief and writes `.benchbook/STATE.md`.
`AfterTool` delivers pending notes and records declared `run_shell_command` checks with readable outcomes.
`benchbook check run NAME` works independently of the tool-output format.

[Hook fields and fixture provenance](../../docs/harnesses.md#gemini-cli)
