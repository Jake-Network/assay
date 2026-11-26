"""CLI: --json output and exit codes (0 ok, 1 user error, 2 duplicate, 3 no task)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def cli(tmp_path: Path, db_url: str):  # type: ignore[no-untyped-def]
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url}
    env.pop("BENCHBOOK_PROJECT", None)

    def run(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "benchbook.cli", *args], capture_output=True, text=True,
                              cwd=tmp_path, env=env, input=input, timeout=60)
    return run


def test_cli_flow(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pid = f"cli-{uuid.uuid4().hex[:6]}"
    r = cli("init", "--project", pid, "--goal", "Pack circles", "--metric", "sum_radii", "--direction", "max",
            "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["project"] == pid
    assert (tmp_path / ".benchbook" / "config.toml").read_text().startswith(f'project = "{pid}"')
    (tmp_path / "cfg.json").write_text('{"init": "hex"}')
    r = cli("exp", "record", "--config", "@cfg.json", "--metrics", '{"sum_radii": 2.5}', "--seed", "0")
    assert r.returncode == 1 and "BENCHBOOK_AGENT" in r.stderr  # writes need a name; no silent 'human' (I1)
    r = cli("--agent", "w0", "exp", "record", "--config", "@cfg.json", "--metrics", '{"sum_radii": 2.5}', "--seed", "0",
            "--json")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["outcome"] == "improved" and rec["is_new_best"]
    r = cli("--agent", "w0", "exp", "record", "--config", "@cfg.json", "--metrics", '{"sum_radii": 2.5}', "--seed", "0",
            "--json")
    assert r.returncode == 2 and json.loads(r.stdout)["duplicate_of"] == rec["id"]
    r = cli("--agent", "w0", "exp", "tried", "--config", "-", "--json", input='{"init": "hex"}')
    assert json.loads(r.stdout)["tried"] is True
    assert cli("--agent", "w0", "task", "next", "--json").returncode == 3
    r = cli("--agent", "human", "task", "add", "Try random", "--tags", "init", "--json")
    tid = json.loads(r.stdout)["ids"][0]
    r = cli("--agent", "w1", "task", "next", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["task"]["lease_owner"] == "w1"
    assert cli("--agent", "human", "task", "done", tid, "-m", "nope").returncode == 1  # human does not hold the lease
    assert cli("--agent", "w1", "task", "done", tid, "-m", "done").returncode == 0
    r = cli("--agent", "w0", "claim", "add", "hypothesis", "hex is good", "--supports", rec["id"], "--json")
    cid = json.loads(r.stdout)["id"]
    r = cli("--agent", "w1", "decide", cid, "supported", "--oracle", "metric", "--rationale", "x")
    assert r.returncode == 1 and "only orchestrator or human" in r.stderr
    r = cli("decide", cid, "supported", "--oracle", "metric", "--rationale", "measured", "--as", "human", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "supported"
    r = cli("sql", "SELECT status FROM v_claim_status", "--json")
    assert json.loads(r.stdout)["rows"] == [{"status": "supported"}]
    r = cli("--agent", "w0", "exp", "record", "--config", '{"a":1}', "--metrics", '{"sum_radii":[1]}', "--json")
    assert r.returncode == 1 and "metrics.sum_radii" in json.loads(r.stdout)["problems"][0]
    r = cli("--agent", "human", "brief")
    assert r.returncode == 0 and r.stdout.startswith(f"# benchbook brief · {pid}")
    r = cli("--agent", "human", "brief", "--budget", "0", "--json")
    assert r.returncode == 1 and "budget" in json.loads(r.stdout)["problems"][0]
    lines = cli("export").stdout.strip().splitlines()
    assert json.loads(lines[0])["type"] == "project" and len(lines) > 5
    assert cli("exp", "record", "--bogus").returncode == 1
    assert cli("get", "e:zzzz").returncode == 1
    r = cli("--agent", "human", "task", "add", "Assigned", "--json")
    t2 = json.loads(r.stdout)["ids"][0]
    r = cli("--agent", "w2", "task", "claim", t2, "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["task"]["lease_owner"] == "w2"
    assert cli("--agent", "w3", "task", "claim", t2).returncode == 3


def test_brief_reads_without_a_name(tmp_path: Path, db_url: str) -> None:
    """brief, like status and get, reads as 'human' when neither --agent nor a name in the environment is given."""
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url}
    for k in ("BENCHBOOK_PROJECT", "BENCHBOOK_AGENT", "ASSAY_AGENT", "BENCHBOOK_TOKEN", "ASSAY_TOKEN"):
        env.pop(k, None)
    pid = f"cli-{uuid.uuid4().hex[:6]}"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "benchbook.cli", *args], capture_output=True, text=True,
                              cwd=tmp_path, env=env, timeout=60)
    assert run("init", "--project", pid, "--metric", "loss", "--direction", "min").returncode == 0
    r = run("brief")
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith(f"# benchbook brief · {pid}")


def test_module_entry_shares_global_options(tmp_path: Path) -> None:
    """`python -m benchbook.cli --json ar ...` must see --json/--project (one STATE, not two)."""
    for mod in ("benchbook", "benchbook.cli"):
        r = subprocess.run([sys.executable, "-m", mod, "--json", "ar", "status"], cwd=tmp_path,
                           capture_output=True, text=True)
        assert r.stdout.startswith("{"), (mod, r.stdout, r.stderr)


# ------------------------------------------------------------------------------------- get within 400 tokens (T12)

def test_get_text_is_bounded_and_json_is_whole(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchbook import Ledger
    from benchbook.core import lookup
    from benchbook.core.tokens import estimate_tokens
    from tests.conftest import make_project

    pid = make_project(db_url)
    lg = Ledger.open(project=pid, agent="w", database_url=db_url)
    c = lg.propose_claim("observation", "a claim with many links " + "x" * 200).id
    for i in range(50):
        e = lg.record_experiment({"i": i}, {"score": float(i)}).id
        lg.link(e, "supports", c)
    got = lg.get(c)
    text = lookup.render(got)
    assert estimate_tokens(text) <= lookup.LIMIT_TOKENS
    assert "links in (50):" in text and "more (--json)" in text and "checked: none" in text
    assert len(got["links_in"]) == 50  # the record itself is whole
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    lookup.log_lookup(pid, got["id"])
    [line] = (tmp_path / "state" / "benchbook" / "lookups.jsonl").read_text().splitlines()
    assert json.loads(line)["id"] == got["id"]
