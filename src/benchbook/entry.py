"""The `benchbook` console script. `benchbook hook <event>` goes straight to the hook adapter without loading the CLI
framework: Claude Code runs it at every subagent stop and Bash call, so its start-up time is paid on each one.
`benchbook check run NAME [--piece P]` takes the same short cut: its start-up is added to every check it wraps.

`assay`, the command's name before the rename, is the same script with a one-line notice (legacy_main)."""
from __future__ import annotations

import sys

# the events the harnesses' hooks run (with an optional --harness NAME); anything else (--help, replay, a typo) goes
# through the CLI
CHECK_HOOK_EVENTS = ("bash-result", "task-completed")  # check_hooks.py: their exit code is the hook's answer
HOOK_EVENTS = ("subagent-stop", "subagent-handback", *CHECK_HOOK_EVENTS)
LEGACY_NOTICE = ("the `assay` command is now `benchbook` (same arguments); `assay` keeps working during 0.1.x. "
                 "Update the scripts, hooks and plugin that still call it: `benchbook doctor` lists what it finds")


def hook_args(argv: list[str]) -> tuple[str, str | None] | None:
    """(event, harness) for `hook EVENT` and `hook EVENT --harness NAME` (or --harness=NAME), the commands the
    manifests in integrations/ run; None for anything else."""
    if len(argv) < 2 or argv[0] != "hook" or argv[1] not in HOOK_EVENTS:
        return None
    rest = argv[2:]
    if not rest:
        return argv[1], None
    if len(rest) == 2 and rest[0] == "--harness":
        return argv[1], rest[1]
    if len(rest) == 1 and rest[0].startswith("--harness="):
        return argv[1], rest[0].partition("=")[2]
    return None


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
    if (hook := hook_args(argv)) is not None:
        event, harness = hook
        if event in CHECK_HOOK_EVENTS:
            from .check_hooks import main as check_hook_main

            sys.exit(check_hook_main(event, harness))
        from .hooks import main as hook_main

        hook_main(event, harness)
        sys.exit(0)
    if argv[:2] == ["check", "run"]:
        # A check's own run pays this start-up (the wrapper's budget is 300 ms); --help and other options go
        # through the CLI and end in the same check_run.run().
        from .check_run import parse_fast_args

        if (fast := parse_fast_args(argv[2:])) is not None:
            from .check_run import run

            sys.exit(run(*fast))
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
