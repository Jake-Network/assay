"""Helpers for the identity tests: the same test database, reached as the benchbook_writer login."""
from __future__ import annotations

import functools
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from benchbook import Ledger, identity

WRITER_PASSWORD = "writer-login-for-tests"


@functools.cache
def as_writer(db_url: str) -> str:
    """Connection string of the writer login. The password is set so this also works where the server asks for
    one (CI service); the embedded test server trusts local connections and ignores it."""
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute(f"ALTER ROLE benchbook_writer PASSWORD '{WRITER_PASSWORD}'")
    return make_conninfo(db_url, user="benchbook_writer", password=WRITER_PASSWORD)


def issue(db_url: str, project: str, name: str, role: str | None = None) -> str:
    """Register the agent (operator action on the owner connection) and return its token."""
    return identity.add_agent(project, name, role, database_url=db_url).token


def token_ledger(db_url: str, project: str, name: str, role: str | None = None) -> Ledger:
    """A ledger handle the way a team member gets one: writer login plus the agent's own token."""
    return Ledger.open(project=project, database_url=as_writer(db_url), token=issue(db_url, project, name, role))


@contextmanager
def writer_conn(db_url: str, project: str | None = None, token: str | None = None) -> Iterator[psycopg.Connection[Any]]:
    """A raw writer connection in one open transaction, authenticated first when a token is given. What an
    agent that ignores the SDK would use."""
    with psycopg.connect(as_writer(db_url)) as conn:
        if token is not None:
            conn.execute("SELECT * FROM benchbook.authenticate(%s, %s)", (project, token))
        yield conn
        conn.rollback()
