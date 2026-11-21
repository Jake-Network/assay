"""`benchbook pr`: the pieces a pull request's commits complete and the evidence summary posted on it (docs/code.md,
"CI evidence and the PR summary"; integrations/github-action calls these). Exit codes: 0 ok, 1 an error."""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

pr_app = typer.Typer(no_args_is_help=True, help="Pull requests: the pieces their commits complete and the evidence "
                                                "summary comment.")

BASE = typer.Option("--base", help="The pull request's base commit (its commits are BASE..HEAD)")
HEAD = typer.Option("--head", help="The pull request's head commit")


@pr_app.command("pieces")
def pieces_cmd(
    base: Annotated[str, BASE],
    head: Annotated[str, HEAD] = "HEAD",
    claims: Annotated[bool, typer.Option(
        "--claims", help="Add a third column: the reporter of the piece's completion claim at that commit, from the "
                         "ledger ('-' when nobody reported it)")] = False,
) -> None:
    """One line per piece a commit of BASE..HEAD names in a Benchbook-Piece trailer: piece, commit (tab separated).
    The last commit carrying a piece's trailer is that piece's commit."""
    from .cli import emit, ledger
    from .pr import pieces, reporter

    found, skipped = pieces(Path.cwd(), base, head)
    _warn(skipped)
    rows: list[dict[str, str | None]] = [{"piece": p.piece, "commit": p.commit} for p in found]
    if claims and found:
        lg = ledger(read_only=True)
        for row, p in zip(rows, found, strict=True):
            by = reporter(lg, p)
            if by is not None and (by.split() != [by] or by == "-"):  # the line is tab separated
                print(f"warning: piece {p.piece}: reporter {by!r} cannot go on a line; left out", file=sys.stderr)
                by = None
            row["reporter"] = by
    emit(rows, "\n".join("\t".join([r["piece"] or "", r["commit"] or ""]
                                    + ([r["reporter"] or "-"] if claims else [])) for r in rows))


@pr_app.command("summary")
def summary_cmd(
    base: Annotated[str, BASE],
    head: Annotated[str, HEAD] = "HEAD",
    run_url: Annotated[str | None, typer.Option("--run-url", help="Link to the CI run, shown in the summary")] = None,
) -> None:
    """Print the markdown summary of BASE..HEAD's pieces: per piece its state (verified, claimed only, re-run
    disagrees, not reported), the runs of its commit, discarded approaches, open out-of-scope findings and whether
    tests changed. It starts with the marker the comment is found by."""
    from .cli import emit, ledger
    from .pr import describe, pieces, render

    found, skipped = pieces(Path.cwd(), base, head)
    _warn(skipped)
    lg = ledger(read_only=True)
    states = describe(lg, found)
    text = render(states, project=lg.project_id, agent=lg.agent, token=lg.token is not None, run_url=run_url,
                  skipped=skipped)
    emit({"pieces": [asdict(s) for s in states], "skipped": skipped, "markdown": text}, text.rstrip("\n"))


@pr_app.command("comment")
def comment_cmd(
    repo: Annotated[str, typer.Option("--repo", help="OWNER/NAME of the repository")],
    pr: Annotated[int, typer.Option("--pr", help="The pull request's number")],
    body_file: Annotated[Path, typer.Option("--body-file", help="The comment (pr summary's output); - reads stdin")],
    author: Annotated[str | None, typer.Option(
        "--author", help="Only a marked comment by this login is ours (e.g. github-actions[bot])")] = None,
    update_only: Annotated[bool, typer.Option(
        "--update-only", help="Update the comment if there is one; never create one")] = False,
) -> None:
    """Create or update the pull request's one benchbook summary comment (the one starting with the marker), with
    the GitHub CLI (gh, authenticated by GH_TOKEN). Extra marked comments by the same author are deleted."""
    from .cli import die, emit
    from .pr import upsert_comment

    try:
        body = sys.stdin.read() if str(body_file) == "-" else body_file.read_text(encoding="utf-8")
    except OSError as e:
        die(f"--body-file: {e}")
    if len(body) > 65000:
        die(f"--body-file: {len(body)} characters; a GitHub comment holds at most 65536")
    what, cid = upsert_comment(repo, pr, body, author=author, update_only=update_only)
    emit({"ok": True, "action": what, "comment": cid},
         f"{what} comment {cid} on {repo}#{pr}" if cid else f"no summary comment on {repo}#{pr}; none created")


def _warn(skipped: list[str]) -> None:
    for raw in skipped:
        print(f"warning: Benchbook-Piece trailer {raw!r} is not a piece name (1-100 characters, no spaces); skipped",
              file=sys.stderr)
