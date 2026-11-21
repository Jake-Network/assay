"""Per-agent tokens through the SDK and the CLI (migration 0008). The forgery attempts against the database
itself are in tests/test_invariants.py (I8)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from benchbook import Ledger, identity
from benchbook.config import default_agent
from benchbook.errors import BenchbookError, NotFound, PermissionDenied

from .conftest import make_project
from .writer import as_writer, issue, token_ledger

Cli = Callable[..., "subprocess.CompletedProcess[str]"]


def _stored_hash(db_url: str, project: str, agent: str) -> str | None:
    with psycopg.connect(db_url) as conn:
        row = conn.execute("SELECT token_hash FROM benchbook.agents WHERE project_id = %s AND id = %s",
                           (project, agent)).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def test_token_is_stored_only_as_its_hash(db_url: str, project: str) -> None:
    t = identity.add_agent(project, "w1", "worker", database_url=db_url)
    assert (t.agent, t.role, t.rotated) == ("w1", "worker", False)
    assert t.token.startswith(identity.TOKEN_PREFIX) and len(t.token) >= 43
    assert _stored_hash(db_url, project, "w1") == identity.token_hash(t.token)
    with psycopg.connect(db_url) as conn:  # the token itself is nowhere in the agent's row
        row = conn.execute("SELECT to_jsonb(a)::text FROM benchbook.agents a WHERE project_id = %s AND id = 'w1'",
                           (project,)).fetchone()
    assert row is not None and t.token not in row[0]
    assert identity.new_token() != identity.new_token()


def test_issuing_fits_agents_registered_on_first_use(db_url: str, project: str) -> None:
    # an agent that already wrote under a self-declared name gets a token for that same row and role
    Ledger.open(project=project, agent="early", role="worker", database_url=db_url).record_experiment({"a": 1})
    assert identity.add_agent(project, "early", database_url=db_url).role == "worker"
    # the agents init_project registers keep their roles; asking for another role is refused, never applied
    assert identity.add_agent(project, "lead", database_url=db_url).role == "orchestrator"
    assert identity.add_agent(project, "human", database_url=db_url).role == "human"
    with pytest.raises(BenchbookError, match="already exists with role harness"):
        identity.add_agent(project, "hook", "orchestrator", database_url=db_url)
    assert _stored_hash(db_url, project, "hook") is None
    assert identity.add_agent(project, "new-worker", database_url=db_url).role == "worker"  # the default
    with pytest.raises(BenchbookError, match="role must be one of"):
        identity.add_agent(project, "x", "admin", database_url=db_url)
    with pytest.raises(NotFound):
        identity.add_agent("no-such-project", "x", database_url=db_url)
    listed = {r["agent"]: r for r in identity.list_agents(project, database_url=db_url)}
    assert listed["early"]["has_token"] and not listed["hook"]["has_token"]
    assert all(set(r) == {"agent", "role", "model", "has_token", "created_at"} for r in listed.values())


def test_rotate_and_revoke(db_url: str, project: str) -> None:
    first = issue(db_url, project, "w1", "worker")
    with pytest.raises(BenchbookError, match="already has a token"):
        identity.add_agent(project, "w1", database_url=db_url)
    assert _stored_hash(db_url, project, "w1") == identity.token_hash(first)  # the refusal changed nothing
    lg = Ledger.open(project=project, database_url=as_writer(db_url), token=first)
    assert lg.record_experiment({"a": 1}).id is not None
    second = identity.add_agent(project, "w1", rotate=True, database_url=db_url)
    assert second.rotated and second.token != first
    with pytest.raises(PermissionDenied, match="revoked or replaced"):  # the open handle stops working at once
        lg.record_experiment({"a": 2})
    with pytest.raises(PermissionDenied):
        Ledger.open(project=project, database_url=as_writer(db_url), token=first)
    lg2 = Ledger.open(project=project, database_url=as_writer(db_url), token=second.token)
    assert lg2.record_experiment({"a": 2}).id is not None
    assert identity.revoke_token(project, "w1", database_url=db_url) is True
    assert identity.revoke_token(project, "w1", database_url=db_url) is False
    with pytest.raises(PermissionDenied):
        lg2.record_experiment({"a": 3})
    with pytest.raises(NotFound):
        identity.revoke_token(project, "nobody", database_url=db_url)
    # issuing, rotating and revoking are the owner's: the writer login may not
    for operator_action in (lambda: identity.add_agent(project, "w9", database_url=as_writer(db_url)),
                            lambda: identity.revoke_token(project, "lead", database_url=as_writer(db_url)),
                            lambda: identity.list_agents(project, database_url=as_writer(db_url))):
        with pytest.raises(PermissionDenied, match="owner's connection"):
            operator_action()


def test_token_names_the_agent(db_url: str, project: str) -> None:
    token = issue(db_url, project, "w1", "worker")
    for url in (db_url, as_writer(db_url)):  # the same rules on the owner's connection and on the writer's
        lg = Ledger.open(project=project, database_url=url, token=token)
        assert (lg.agent, lg.role) == ("w1", "worker")
        assert Ledger.open(project=project, agent="w1", database_url=url, token=token).agent == "w1"
        assert lg.as_agent("w1") is lg
        with pytest.raises(PermissionDenied, match="belongs to agent w1"):
            Ledger.open(project=project, agent="lead", database_url=url, token=token)
        with pytest.raises(PermissionDenied, match="bound to agent w1"):
            lg.as_agent("w2")
        with pytest.raises(PermissionDenied, match="not valid for project"):
            Ledger.open(project=project, database_url=url, token="bbt_not-a-token")
        other = make_project(db_url)
        with pytest.raises(PermissionDenied, match="not valid for project"):
            Ledger.open(project=other, database_url=url, token=token)


def test_token_comes_from_the_environment(db_url: str, project: str, monkeypatch: pytest.MonkeyPatch) -> None:
    token = issue(db_url, project, "w1", "worker")
    monkeypatch.delenv("BENCHBOOK_AGENT", raising=False)
    monkeypatch.setenv("BENCHBOOK_TOKEN", token)
    assert default_agent("hook") is None  # adapters stop asking for their fixed names
    lg = Ledger.open(project=project, database_url=as_writer(db_url))
    assert lg.agent == "w1" and lg.token == token
    monkeypatch.delenv("BENCHBOOK_TOKEN")
    monkeypatch.setenv("ASSAY_TOKEN", token)  # the pre-rename variable name still works (config.env)
    assert Ledger.open(project=project, database_url=as_writer(db_url)).agent == "w1"
    monkeypatch.delenv("ASSAY_TOKEN")
    assert default_agent("hook") == "hook"
    with pytest.raises(BenchbookError, match="BENCHBOOK_AGENT"):
        Ledger.open(project=project, database_url=as_writer(db_url))


def test_mcp_server_with_a_token_is_one_agent(db_url: str, project: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchbook.mcp_server import Server

    monkeypatch.setenv("BENCHBOOK_DATABASE_URL", as_writer(db_url))
    monkeypatch.setenv("BENCHBOOK_TOKEN", issue(db_url, project, "w1", "worker"))
    srv = Server(project)
    assert srv.lg("w1").record_experiment({"a": 1}).id is not None
    for name in ("lead", "w2"):  # a caller cannot pick another name by passing it as the agent argument
        with pytest.raises(PermissionDenied, match="bound to agent w1"):
            srv.lg(name)


def test_export_leaves_out_token_hashes(db_url: str, project: str) -> None:
    token = issue(db_url, project, "w1", "worker")
    for lg in (Ledger.open(project=project, agent="lead", database_url=db_url), token_ledger(db_url, project, "w2")):
        dump = json.dumps(list(lg.export()), default=str)
        assert '"w1"' in dump and "token_hash" not in dump and identity.token_hash(token) not in dump


def test_notify_mode_records_a_repeat_over_the_writer_login(db_url: str, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """dedup_mode = "notify" (migration 0009) through the SDK with a token: the repeat is stored under the
    token's agent, and 0009's trigger, which reads experiments with the caller's privileges, lets it through."""
    from benchbook import ledger as ledger_module
    from benchbook import packs

    from .test_dedup_mode import NOTIFY_PACK, _pack_dir

    pack = packs.read_pack(NOTIFY_PACK, _pack_dir(tmp_path, "notify"))
    real = packs.load_pack
    monkeypatch.setattr(ledger_module, "load_pack", lambda name: pack if name == NOTIFY_PACK else real(name))
    pid = make_project(db_url, pack=NOTIFY_PACK)
    first = token_ledger(db_url, pid, "w1", "worker").record_experiment({"a": 1}, {"score": 1.0}, seed=0)
    again = token_ledger(db_url, pid, "w2", "worker").record_experiment({"a": 1}, {"score": 1.0}, seed=0)
    assert again.duplicate_of == first.id and again.id not in (None, first.id)
    with psycopg.connect(db_url) as conn:
        rows = conn.execute("SELECT author, duplicate_of FROM benchbook.experiments WHERE project_id = %s"
                            " ORDER BY created_at", (pid,)).fetchall()
        hits = conn.execute("SELECT kind, agent_id FROM benchbook.dedup_hits WHERE project_id = %s", (pid,)).fetchall()
    assert rows == [("w1", None), ("w2", first.id)] and hits == [("notified", "w2")]


# CLI ---------------------------------------------------------------------------------------------------

@pytest.fixture
def cli(tmp_path: Path, db_url: str) -> Cli:
    base = {k: v for k, v in os.environ.items() if k not in ("BENCHBOOK_PROJECT", "BENCHBOOK_AGENT", "BENCHBOOK_TOKEN")}

    def run(*args: str, url: str = db_url, token: str | None = None) -> subprocess.CompletedProcess[str]:
        env = {**base, "BENCHBOOK_DATABASE_URL": url, **({"BENCHBOOK_TOKEN": token} if token else {})}
        return subprocess.run([sys.executable, "-m", "benchbook.cli", *args], capture_output=True, text=True,
                              cwd=tmp_path, env=env, timeout=60)
    return run


def test_cli_agent_add_prints_the_token_exactly_once(cli: Cli, db_url: str) -> None:
    pid = f"tok-{uuid.uuid4().hex[:6]}"
    assert cli("init", "--project", pid).returncode == 0
    r = cli("agent", "add", "w1", "--role", "worker", "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    token = out["token"]
    assert (out["agent"], out["role"], out["project"], out["rotated"]) == ("w1", "worker", pid, False)
    assert r.stdout.count(token) == 1 and token not in r.stderr
    assert _stored_hash(db_url, pid, "w1") == identity.token_hash(token)
    # no later command can show it again
    again = cli("agent", "add", "w1", "--role", "worker")
    assert again.returncode == 1 and "already has a token" in again.stderr and "bbt_" not in again.stdout + again.stderr
    listing = cli("agent", "list", "--json")
    rows = {row["agent"]: row for row in json.loads(listing.stdout)}
    assert rows["w1"]["has_token"] is True and rows["lead"]["has_token"] is False
    assert token not in listing.stdout and identity.token_hash(token) not in listing.stdout
    assert "token" in cli("agent", "list").stdout and token not in cli("agent", "list").stdout
    # text output: the token on one line, once, with the variable to set
    text = cli("agent", "add", "w2")
    assert text.returncode == 0 and "(worker)" in text.stdout and "BENCHBOOK_TOKEN=bbt_" in text.stdout
    assert sum(line.startswith("token: bbt_") for line in text.stdout.splitlines()) == 1
    rotated = json.loads(cli("agent", "add", "w1", "--rotate", "--json").stdout)
    assert rotated["rotated"] is True and rotated["token"] != token
    assert _stored_hash(db_url, pid, "w1") == identity.token_hash(rotated["token"])
    wrong_role = cli("agent", "add", "lead", "--role", "worker")
    assert wrong_role.returncode == 1 and "keeps its role" in wrong_role.stderr
    assert json.loads(cli("agent", "revoke", "w1", "--json").stdout) == {"ok": True, "agent": "w1", "revoked": True}
    assert _stored_hash(db_url, pid, "w1") is None


def test_cli_reads_benchbook_token(cli: Cli, db_url: str) -> None:
    pid = f"tok-{uuid.uuid4().hex[:6]}"
    assert cli("init", "--project", pid, "--metric", "score", "--direction", "max").returncode == 0
    worker = json.loads(cli("agent", "add", "w1", "--json").stdout)["token"]
    lead = json.loads(cli("agent", "add", "lead", "--json").stdout)["token"]
    writer = as_writer(db_url)
    record = ("exp", "record", "--config", '{"a": 1}', "--metrics", '{"score": 1.0}', "--json")
    # the writer login without a token: named or not, nothing is recorded
    for args in (record, ("--agent", "w1", *record), ("--agent", "lead", *record)):
        r = cli(*args, url=writer)
        assert r.returncode == 1 and ("BENCHBOOK_TOKEN" in r.stdout or "BENCHBOOK_AGENT" in r.stdout), r.stdout
    # with the token: no name needed, and no other name accepted
    r = cli(*record, url=writer, token=worker)
    assert r.returncode == 0, r.stdout + r.stderr
    exp = json.loads(r.stdout)["id"]
    r = cli("--agent", "lead", *record, url=writer, token=worker)
    assert r.returncode == 1 and "belongs to agent w1" in r.stdout
    assert cli("status", url=writer, token=worker).returncode == 0  # read-only commands do not ask for 'human'
    claim = json.loads(cli("claim", "add", "observation", "a=1 scores 1", "--json", url=writer,
                           token=worker).stdout)["id"]
    # deciding: the worker's token cannot, under its own name or the lead's or a person's
    for who in ((), ("--as", "human"), ("--as", "lead")):
        r = cli("decide", claim, "supported", "--oracle", "metric", "--rationale", "x", "--evidence", exp, *who,
                url=writer, token=worker)
        assert r.returncode == 1, r.stdout
    r = cli("decide", claim, "supported", "--oracle", "metric", "--rationale", "measured", "--evidence", exp,
            "--json", url=writer, token=lead)
    assert r.returncode == 0, r.stdout + r.stderr
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT author FROM benchbook.experiments WHERE project_id = %s", (pid,)).fetchall() \
            == [("w1",)]
        assert conn.execute("SELECT decided_by FROM benchbook.decisions WHERE project_id = %s", (pid,)).fetchall() \
            == [("lead",)]
    # operator commands are refused on the writer login, whatever token it holds
    r = cli("agent", "add", "w3", url=writer, token=lead)
    assert r.returncode == 1 and "owner's connection" in r.stderr and "bbt_" not in r.stdout
