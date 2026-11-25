"""`benchbook agent`: register agents and issue, list and revoke their tokens. Operator commands: they change
agents.token_hash, which only the database owner's connection may do (identity.py, docs/multi-machine.md)."""
from __future__ import annotations

from typing import Annotated, Any

import typer

agent_app = typer.Typer(no_args_is_help=True,
                        help="Agents and their tokens (operator commands; need the owner's database URL).")


def _project() -> str:
    from . import config
    from .cli import STATE, die

    project: str | None = STATE["project"] or config.load().project
    if not project:
        die("no project: pass --project, set BENCHBOOK_PROJECT, or run `benchbook init`")
    return project


def _emit(data: Any, text: str) -> None:
    from .cli import emit

    emit(data, text)


@agent_app.command("add")
def add(name: str,
        role: Annotated[str | None, typer.Option(help="worker (default for a new agent), orchestrator, human or "
                                                      "harness; an existing agent keeps its role")] = None,
        model: Annotated[str | None, typer.Option(help="Model name, recorded for a new agent")] = None,
        rotate: Annotated[bool, typer.Option("--rotate", help="Replace the agent's token; the old one stops "
                                                              "working at once")] = False) -> None:
    """Register an agent (if new) and print its token. The token is shown this once: only its hash is stored."""
    from .core.identity import add_agent

    t = add_agent(_project(), name, role, model=model, rotate=rotate)
    _emit({"ok": True, "project": t.project, "agent": t.agent, "role": t.role, "rotated": t.rotated,
           "token": t.token},
          f"agent {t.agent} ({t.role}) in project {t.project}" + (": token replaced" if t.rotated else "") + "\n"
          f"token: {t.token}\n"
          "This is the only time the token is shown; benchbook keeps its SHA-256 and nothing else.\n"
          f"On the agent's machine: export BENCHBOOK_TOKEN={t.token}")


@agent_app.command("list")
def list_() -> None:
    """Agents of the project: role and whether a token is issued (never the token or its hash)."""
    from .core.identity import list_agents

    rows = list_agents(_project())
    _emit(rows, "\n".join(f"{r['agent']:<24} {r['role']:<13} {'token' if r['has_token'] else '-'}" for r in rows)
          or "no agents")


@agent_app.command("revoke")
def revoke(name: str) -> None:
    """Remove an agent's token. Its records stay; it can no longer write over a benchbook_writer connection."""
    from .core.identity import revoke_token

    had = revoke_token(_project(), name)
    _emit({"ok": True, "agent": name, "revoked": had},
          f"revoked the token of {name}" if had else f"{name} had no token")
