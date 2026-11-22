"""Per-agent tokens (migration 0008): issue, check and revoke them.

A token is a random secret an operator issues once for one agent of one project. Only its SHA-256 is stored, so
neither the database nor a dump of it can be used to act as the agent. A connection made as the benchbook_writer
login can write only as the agent whose token it presented; what that does and does not guarantee is spelled
out in docs/multi-machine.md.
"""
from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg

from .errors import BenchbookError, InvalidAgentToken, NotFound, PermissionDenied

TOKEN_PREFIX = "bbt_"  # recognisable in a secret scanner; carries no information
_OWNER_ONLY = ("this needs the database owner's connection: the benchbook_writer login cannot register agents or "
               "touch tokens. Run it where BENCHBOOK_DATABASE_URL is the owner's URL.")


@dataclass(frozen=True)
class IssuedToken:
    project: str
    agent: str
    role: str
    token: str      # shown to the operator once; never stored
    rotated: bool   # an earlier token of this agent stopped working


def new_token() -> str:
    # 256 random bits: guessing is not an attack, so a plain SHA-256 (no salt, no slow hash) is enough.
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """The value stored in agents.token_hash; the database computes the same from a presented token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(conn: psycopg.Connection[Any], project_id: str, token: str) -> tuple[str, str]:
    """Bind the open transaction on conn to the token's agent; returns (agent, role). The token goes as a bind
    parameter: statement text is visible in pg_stat_activity to other sessions of the same login."""
    try:
        row = conn.execute("SELECT agent_id, agent_role FROM benchbook.authenticate(%s, %s)",
                           (project_id, token)).fetchone()
    except psycopg.errors.InvalidPassword as e:
        raise InvalidAgentToken(
            f"BENCHBOOK_TOKEN is not valid for project {project_id}: it was revoked or replaced, or it was issued "
            "for another project. Ask the operator for a new one (benchbook agent add NAME --rotate).") from e
    assert row is not None
    return str(row["agent_id"]), str(row["agent_role"])


@contextmanager
def session(pool: Any, project_id: str, token: str) -> Iterator[Any]:
    """One transaction on a pooled connection, acting as the token's agent. The identity is transaction-local,
    so the connection goes back to the pool with none: the next checkout, whoever makes it, starts unbound."""
    try:
        with pool.connection() as conn:
            authenticate(conn, project_id, token)
            yield conn
    except psycopg.errors.InsufficientPrivilege as e:
        # raised by the identity triggers (or a missing grant); the message says which rule
        raise PermissionDenied(e.diag.message_primary or str(e)) from e


# ------------------------------------------------------------------ operator actions (owner connection)

def _owner_conn(database_url: str | None) -> Any:
    from .ledger import Ledger

    return Ledger._pool(Ledger.ensure_schema(database_url)).connection()


def add_agent(project: str, name: str, role: str | None = None, *, model: str | None = None,
              rotate: bool = False, database_url: str | None = None) -> IssuedToken:
    """Register the agent if it is new (role defaults to worker) and issue its token. An agent that already
    has a token keeps it unless rotate=True, which replaces it: the old token stops working at once."""
    from .ledger import Ledger

    token = new_token()
    try:
        with _owner_conn(database_url) as conn:
            if conn.execute("SELECT 1 FROM benchbook.projects WHERE id = %s", (project,)).fetchone() is None:
                raise NotFound(f"project {project!r} does not exist; run benchbook init --project {project}")
            actual = Ledger._ensure_agent(conn, project, name, role, model)
            if role is not None and actual != role:
                raise BenchbookError(f"agent {name} already exists with role {actual}; an agent keeps its role (P2). "
                                     f"Pass --role {actual}, or choose another name.")
            had = conn.execute("SELECT token_hash IS NOT NULL AS had FROM benchbook.agents"
                               " WHERE project_id = %s AND id = %s FOR UPDATE", (project, name)).fetchone()["had"]
            if had and not rotate:
                raise BenchbookError(f"agent {name} already has a token, and it cannot be shown again. "
                                     "--rotate issues a new one; the old one stops working at once.")
            conn.execute("UPDATE benchbook.agents SET token_hash = %s WHERE project_id = %s AND id = %s",
                         (token_hash(token), project, name))
    except psycopg.errors.InsufficientPrivilege as e:
        raise PermissionDenied(_OWNER_ONLY) from e
    return IssuedToken(project=project, agent=name, role=actual, token=token, rotated=bool(had))


def revoke_token(project: str, name: str, database_url: str | None = None) -> bool:
    """Remove the agent's token; returns whether it had one. Its records stay (append-only); the agent can
    no longer write over a benchbook_writer connection until a new token is issued."""
    try:
        with _owner_conn(database_url) as conn:
            row = conn.execute("SELECT token_hash IS NOT NULL AS had FROM benchbook.agents"
                               " WHERE project_id = %s AND id = %s FOR UPDATE", (project, name)).fetchone()
            if row is None:
                raise NotFound(f"no agent {name!r} in project {project}")
            conn.execute("UPDATE benchbook.agents SET token_hash = NULL WHERE project_id = %s AND id = %s",
                         (project, name))
    except psycopg.errors.InsufficientPrivilege as e:
        raise PermissionDenied(_OWNER_ONLY) from e
    return bool(row["had"])


def list_agents(project: str, database_url: str | None = None) -> list[dict[str, Any]]:
    """Every agent of the project with its role and whether it holds a token (never the hash)."""
    try:
        with _owner_conn(database_url) as conn:
            rows: list[dict[str, Any]] = conn.execute(
                "SELECT id AS agent, role, model, token_hash IS NOT NULL AS has_token, created_at"
                " FROM benchbook.agents WHERE project_id = %s ORDER BY created_at, id", (project,)).fetchall()
    except psycopg.errors.InsufficientPrivilege as e:
        raise PermissionDenied(_OWNER_ONLY) from e
    return rows
