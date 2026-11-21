"""Search lines end with tags; --tags matches any tag, --all-tags every tag (CLI, SDK, MCP)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from benchbook import Ledger
from benchbook.config import write_config
from benchbook.mcp_server import build
from tests.test_mcp import call


def _claims(worker: Ledger) -> dict[str, str]:
    ids = {}
    for name, tags in [("both", ["mod:ingest", "abandoned"]), ("mod-only", ["mod:ingest", "out-of-scope"]),
                       ("abandoned-only", ["mod:export", "abandoned"])]:
        ids[name] = "c:" + str(worker.propose_claim("note", f"claim {name}", tags=tags).id)[:8]
    return ids


def test_any_versus_all_tags(worker: Ledger) -> None:
    ids = _claims(worker)
    want = ["mod:ingest", "abandoned"]
    anyof = {r["id"] for r in worker.search("", "claim", tags=want)}
    allof = worker.search("", "claim", all_tags=want)
    assert anyof == set(ids.values())
    assert [r["id"] for r in allof] == [ids["both"]]
    assert allof[0]["line"].endswith("[tags: mod:ingest, abandoned]")
    assert allof[0]["tags"] == want
    # both filters together: any of the first set and all of the second
    both = worker.search("", "claim", tags=["out-of-scope", "abandoned"], all_tags=["mod:ingest"])
    assert {r["id"] for r in both} == {ids["both"], ids["mod-only"]}


def test_mcp_and_cli_all_tags(worker: Ledger, project: str, tmp_path: Path, db_url: str) -> None:
    ids = _claims(worker)
    rows = call(build(project, "all"), "benchbook_search", agent="lead", text="", kind="claim",
                all_tags=["mod:ingest", "abandoned"])
    assert [r["id"] for r in rows] == [ids["both"]]
    assert rows[0]["line"].endswith("[tags: mod:ingest, abandoned]")
    write_config(tmp_path, project)
    env = {**os.environ, "BENCHBOOK_DATABASE_URL": db_url}
    env.pop("BENCHBOOK_PROJECT", None)

    def cli(*args: str) -> str:
        p = subprocess.run([sys.executable, "-m", "benchbook.cli", "search", "", "--kind", "claim", *args],
                           capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60)
        assert p.returncode == 0, p.stderr
        return p.stdout

    out_any = cli("--tags", "mod:ingest,abandoned")
    out_all = cli("--all-tags", "mod:ingest,abandoned")
    assert all(i in out_any for i in ids.values())
    assert out_all.strip().splitlines() == [ln for ln in out_any.strip().splitlines() if ln.startswith(ids["both"])]
    assert "[tags: mod:ingest, out-of-scope]" in out_any
    assert json.loads(subprocess.run([sys.executable, "-m", "benchbook.cli", "--json", "search", "", "--all-tags",
                                      "abandoned,mod:export"], capture_output=True, text=True, cwd=tmp_path,
                                     env=env, timeout=60).stdout)[0]["id"] == ids["abandoned-only"]
