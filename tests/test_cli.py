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
    env = {**os.environ, "ASSAY_DATABASE_URL": db_url}
    env.pop("ASSAY_PROJECT", None)

    def run(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "assay.cli", *args], capture_output=True, text=True,
                              cwd=tmp_path, env=env, input=input, timeout=60)
    return run


def test_cli_flow(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    pid = f"cli-{uuid.uuid4().hex[:6]}"
    r = cli("init", "--project", pid, "--goal", "Pack circles", "--metric", "sum_radii", "--direction", "max",
            "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["project"] == pid
    assert (tmp_path / ".assay" / "config.toml").read_text().startswith(f'project = "{pid}"')
    (tmp_path / "cfg.json").write_text('{"init": "hex"}')
    r = cli("exp", "record", "--config", "@cfg.json", "--metrics", '{"sum_radii": 2.5}', "--seed", "0", "--json")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["outcome"] == "improved" and rec["is_new_best"]
    r = cli("exp", "record", "--config", "@cfg.json", "--metrics", '{"sum_radii": 2.5}', "--seed", "0", "--json")
    assert r.returncode == 2 and json.loads(r.stdout)["duplicate_of"] == rec["id"]
    r = cli("exp", "tried", "--config", "-", "--json", input='{"init": "hex"}')
    assert json.loads(r.stdout)["tried"] is True
    assert cli("task", "next", "--json").returncode == 3
    r = cli("task", "add", "Try random", "--tags", "init", "--json")
    tid = json.loads(r.stdout)["ids"][0]
    r = cli("--agent", "w1", "task", "next", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["task"]["lease_owner"] == "w1"
    assert cli("task", "done", tid, "-m", "nope").returncode == 1  # human does not hold the lease
    assert cli("--agent", "w1", "task", "done", tid, "-m", "done").returncode == 0
    r = cli("claim", "add", "hypothesis", "hex is good", "--supports", rec["id"], "--json")
    cid = json.loads(r.stdout)["id"]
    r = cli("--agent", "w1", "decide", cid, "supported", "--oracle", "metric", "--rationale", "x")
    assert r.returncode == 1 and "only orchestrator or human" in r.stderr
    r = cli("decide", cid, "supported", "--oracle", "metric", "--rationale", "measured", "--as", "human", "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["status"] == "supported"
    r = cli("sql", "SELECT status FROM v_claim_status", "--json")
    assert json.loads(r.stdout)["rows"] == [{"status": "supported"}]
    r = cli("exp", "record", "--config", '{"a":1}', "--metrics", '{"sum_radii":[1]}', "--json")
    assert r.returncode == 1 and "metrics.sum_radii" in json.loads(r.stdout)["problems"][0]
    r = cli("brief")
    assert r.returncode == 0 and r.stdout.startswith(f"# assay brief · {pid}")
    lines = cli("export").stdout.strip().splitlines()
    assert json.loads(lines[0])["type"] == "project" and len(lines) > 5
    assert cli("exp", "record", "--bogus").returncode == 1
    assert cli("get", "e:zzzz").returncode == 1
