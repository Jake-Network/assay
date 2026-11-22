# Benchbook hooks for OpenCode V2

Install `benchbook` on the OpenCode server's PATH and run `benchbook init` in the checkout. Copy
[index.ts](index.ts) to the checkout's `.opencode/plugins/benchbook/index.ts` and reload or restart OpenCode.
Use the OpenCode V2 plugin SDK described in [Building plugins](https://opencode.ai/docs/build/plugins/).

The plugin adds the scoped brief to the first model request and carries that snapshot in subsequent requests
from the same session and checkout. It delivers pending notes on the next model request and observes declared
checks run by OpenCode's foreground `shell` tool. To uninstall, remove the plugin file.

[Hook fields and fixture provenance](../../docs/harnesses.md#opencode-v2)
