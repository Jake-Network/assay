"""`benchbook check run` as a CLI command. The work is check_run.run, which the console script also calls directly
(entry.py) for the common spelling; this is the path for global options and --help."""
from __future__ import annotations

from typing import Annotated

import typer


def run(
    name: Annotated[str, typer.Argument(help="The declared check ([checks.<name>] in .benchbook/config.toml)")],
    piece: Annotated[str | None, typer.Option(
        "--piece", help="The piece this run belongs to (default: BENCHBOOK_PIECE, else the current branch)")] = None,
    approach: Annotated[str | None, typer.Option(
        "--approach", help="The approach being tried, recorded as config.approach (default: BENCHBOOK_APPROACH)")]
    = None,
) -> None:
    """Run the declared check NAME from the current checkout and record the run.

    The check runs from the repository root through the shell, as config.toml
    declares it, with stdin closed. Its stdout and stderr pass through
    unchanged on stdout, in the order the command wrote them, as cmd 2>&1
    would print them.

    What check run adds comes after the output, on stderr: one line that starts
    with "benchbook: " and gives the result, the piece, the commit and where the
    run was queued, e.g. "benchbook: unit ok (exit 0, 1.2s), piece media at
    1a2b3c4d5e6f; queued for the ledger (...)".

    After a failure the run is signed (failing JUnit test, TypeScript error
    codes and files, or the exit code and a hash of the last error line), and
    when an earlier attempt of the same piece failed the same way a second line
    names it: "benchbook: seen before: piece media failed the same way in
    e:1a2b3c4d (impl-a, 2h ago, approach hash-before-put)...". It is a notice;
    nothing is refused. The lookup waits at most 2 seconds and says nothing
    when the ledger does not answer. A passing run never queries the ledger.

    Recording: the run is queued in the local hook spool and a background
    process writes it to the ledger as an attempt (config piece and check,
    code_ref the HEAD commit, metrics as check verify writes them, plus
    via=check-run). A database that is down never changes the exit code; the run
    stays queued and "benchbook hook replay" records it. With uncommitted or
    untracked files the run is recorded with worktree_dirty=true: it is not a
    run of that commit, and the completion gate does not count it. A failed run
    of a clean checkout also leaves a discarded-approach claim (docs/code.md).

    Exit code: the check's own; 124 after its timeout; 128+N after signal N
    (130 for Ctrl-C); 2 when the check did not start (no such check, no
    project, no piece, not in a git repository), with the reason on stderr and
    nothing recorded."""
    from .checks.run import run as run_check
    from .cli import STATE

    raise typer.Exit(run_check(name, piece, approach, project=STATE["project"], agent=STATE["agent"]))
