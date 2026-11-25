"""`benchbook piece`: start a piece of coding work on the paths it will change, release it, list who holds what
(docs/code.md, "Path occupancy"). Exit codes: 0 ok, 1 user error, 2 refused (another live piece holds the piece or
overlapping paths; the message names its task id)."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from .pieces import Started

piece_app = typer.Typer(no_args_is_help=True, help="Pieces of coding work and the paths they occupy.")
SHOWN = 20


@piece_app.command("start")
def start(
    piece: Annotated[str, typer.Argument(help="The piece's name (the same name check verify takes)")],
    paths: Annotated[list[str] | None, typer.Option(
        "--paths", help="A path glob the piece will change, from the repository root (repeatable). Required the "
                        "first time; on a restart it replaces the piece's paths, and without it they stay.")] = None,
    lease: Annotated[int | None, typer.Option(
        "--lease", help="Seconds the paths stay occupied without another start (default 7200)")] = None,
    tags: Annotated[str | None, typer.Option(
        help="Extra tags for the piece, e.g. mod:media (comma separated; fixed at the first start)")] = None,
    check: Annotated[list[str] | None, typer.Option(
        "--check", help="NAME=CMD: pin this piece's completion check in the ledger (repeatable; benchbook check "
                        "pin)")] = None,
) -> None:
    """Occupy PATHS for PIECE. Refused (exit 2) when a live piece holds the same piece or paths that overlap; run it
    again to renew the lease. --check pins the piece's checks before anyone works on it."""
    from .cli import csv, emit, ledger
    from .pieces import start as run_start

    lg = ledger()
    specs = None
    if check:
        from .checks import parse_check_options

        specs = parse_check_options(check)  # a malformed --check fails before the piece is started
    s = run_start(lg, piece, paths or [], Path.cwd(), lease_seconds=lease, tags=csv(tags))
    if s.note is not None:
        import sys

        print(f"note: {s.note}", file=sys.stderr)
    out, text = summary(s), render(s)
    if specs:
        from .checks import pin_checks
        from .config import load
        from .core.ids import short

        cfg = load()
        p, new = pin_checks(lg, piece, specs, cfg.guard_paths, cfg.verify.table())
        out["pin"] = short("claim", p.id)
        text += f"\n{'pinned' if new else 'already pinned'} checks {', '.join(p.checks)} ({out['pin']})"
    emit(out, text)


@piece_app.command("done")
def done(
    piece: Annotated[str, typer.Argument(help="The piece to release")],
    failed: Annotated[bool, typer.Option("--failed", help="Record it as failed rather than done")] = False,
    summary_text: Annotated[str, typer.Option("--summary", "-m")] = "",
) -> None:
    """Release PIECE's paths: its holder marks it done (or failed); a lead releasing another agent's piece cancels
    it."""
    from .cli import emit, ledger
    from .pieces import done as run_done

    t = run_done(ledger(), piece, failed, summary_text)
    emit({"ok": True, "piece": piece, "task": t.short_id, "status": t.status},
         f"released piece {piece} ({t.short_id}, {t.status})")


@piece_app.command("list")
def list_(
    tags: Annotated[str | None, typer.Option(help="Only pieces with all of these tags (comma separated)")] = None,
    all_paths: Annotated[bool, typer.Option("--all-paths", help="Print every occupied path")] = False,
) -> None:
    """Live pieces: who holds which paths, and for how long."""
    from .cli import csv, emit, ledger
    from .pieces import held

    rows = held(ledger(read_only=True), csv(tags))
    lines: list[str] = []
    for r in rows:
        extra = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
        lines.append(f"{r['task']}  {r['piece']}  {r['agent']}  {_left(r['lease_left_s'])} left{extra}  "
                     f"{', '.join(r['declared'])}")
        lines += _paths(r["paths"], None if all_paths else SHOWN)
    emit(rows, "\n".join(lines) or "(no live pieces)")


def summary(s: Started) -> dict[str, Any]:
    t = s.task
    return {"ok": True, "piece": t.spec.get("piece"), "task": t.short_id, "agent": t.lease_owner,
            "renewed": s.renewed, "paths": s.paths, "new": s.new, "declared": t.spec.get("declared", []),
            "lease_expires_at": t.lease_expires_at, "note": s.note}


def render(s: Started) -> str:
    t = s.task
    left = (t.lease_expires_at - t.updated_at).total_seconds() if t.lease_expires_at else 0
    head = (f"{'renewed' if s.renewed else 'started'} piece {t.spec.get('piece')} ({t.short_id}) for "
            f"{t.lease_owner}: {len(s.paths)} path(s), lease {_left(left)}")
    lines = [head, *_paths(s.paths, SHOWN)]
    if s.new:
        lines.append("no tracked file yet (kept as globs): " + ", ".join(s.new))
    return "\n".join(lines)


def _paths(paths: list[str], limit: int | None) -> list[str]:
    shown = paths if limit is None else paths[:limit]
    out = [f"    {p}" for p in shown]
    if len(paths) > len(shown):
        out.append(f"    ... and {len(paths) - len(shown)} more (--all-paths, or --json)")
    return out


def _left(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"
