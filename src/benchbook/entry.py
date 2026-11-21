"""The `benchbook` console script. `benchbook hook <event>` goes straight to the hook adapter without loading the CLI
framework: Claude Code runs it at every subagent stop, so its start-up time is paid on each one.

`assay`, the command's name before the rename, is the same script with a one-line notice (legacy_main)."""
from __future__ import annotations

import sys

# the events Claude Code's hooks run; anything else (--help, replay, a typo) goes through the CLI
HOOK_EVENTS = ("subagent-stop", "subagent-handback")
LEGACY_NOTICE = ("the `assay` command is now `benchbook` (same arguments); `assay` keeps working during 0.1.x. "
                 "Update the scripts, hooks and plugin that still call it: `benchbook doctor` lists what it finds")


def main(legacy: bool = False) -> None:
    argv = sys.argv[1:]
    if legacy:
        from . import config

        config.LEGACY_COMMAND = True
        # A hook run by Claude Code must not write to stderr, whichever way it is spelled (`assay --project p hook
        # subagent-stop` goes through the CLI): hooks.main writes the held notice to hook.log instead. Only
        # `hook replay`, which a person runs in a terminal, is told like any other command.
        if "hook" in argv and argv[argv.index("hook") + 1:][:1] != ["replay"]:
            config.hold_notices()
        config.legacy_notice("command", LEGACY_NOTICE)
    if len(argv) == 2 and argv[0] == "hook" and argv[1] in HOOK_EVENTS:
        from .hooks import main as hook_main

        hook_main(argv[1])
        sys.exit(0)
    if argv in (["--version"], ["-V"]):
        from . import __version__

        print(f"benchbook {__version__}")
        sys.exit(0)
    from .cli import main as cli_main

    cli_main()


def legacy_main() -> None:
    """The `assay` console script: say once on stderr that the command was renamed, then run as `benchbook`.
    Hooks deployed before the rename call `assay hook subagent-stop`; there the notice goes to the hook log."""
    main(legacy=True)
