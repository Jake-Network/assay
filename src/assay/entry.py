"""The `assay` console script. `assay hook <event>` goes straight to the hook adapter without loading the CLI
framework: Claude Code runs it at every subagent stop, so its start-up time is paid on each one."""
from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == "hook" and argv[1] != "replay":
        from .hooks import main as hook_main

        hook_main(argv[1])
        sys.exit(0)
    if argv in (["--version"], ["-V"]):
        from . import __version__

        print(f"assay {__version__}")
        sys.exit(0)
    from .cli import main as cli_main

    cli_main()
