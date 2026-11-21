"""MCP adapter: toolsets, round trip, error messages, schema resource."""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from benchbook import Ledger
from benchbook.config import write_config
from benchbook.mcp_server import build, tool_names
from tests.conftest import make_project


def call(mcp: Any, name: str, **args: Any) -> Any:
    res = asyncio.run(mcp.call_tool(name, args))
    if hasattr(res, "is_error"):  # CallToolResult
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        if res.is_error:
            raise RuntimeError(text)
        sc = res.structured_content
        if sc is not None:
            return sc["result"] if set(sc) == {"result"} else sc
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if isinstance(res, tuple):
        res = res[1] if res[1] is not None else res[0]
    if isinstance(res, dict) and set(res) == {"result"}:
        return res["result"]
    if isinstance(res, list) and res and hasattr(res[0], "text"):
        txt = res[0].text
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            return txt
    return res


def test_toolsets(project: str) -> None:
    worker = tool_names(build(project, "worker"))
    assert len(worker) == 9 and "benchbook_decide" not in worker and "benchbook_create_tasks" not in worker
    full = tool_names(build(project, "all"))
    assert {"benchbook_decide", "benchbook_create_tasks", "benchbook_cancel_task", "benchbook_status", "benchbook_sql",
            "benchbook_search"} <= set(full)


def test_round_trip(project: str) -> None:
    mcp = build(project, "all")
    out = call(mcp, "benchbook_create_tasks", agent="lead", tasks=[{"title": "hex", "tags": ["init"]}])
    tid = out["ids"][0]
    t = call(mcp, "benchbook_next_task", agent="worker-1")
    assert t["task"]["id"] == tid
    assert call(mcp, "benchbook_check_tried", agent="worker-1", config={"init": "hex"})["tried"] is False
    r = call(mcp, "benchbook_record_experiment", agent="worker-1", config={"init": "hex"}, metrics={"score": 2.5},
             seed=0, task_id=tid)
    assert r["outcome"] == "improved" and r["id"].startswith("e:")
    dup = call(mcp, "benchbook_record_experiment", agent="worker-1", config={"init": "hex"}, metrics={"score": 2.5},
               seed=0)
    assert dup["duplicate_of"] == r["id"]
    c = call(mcp, "benchbook_propose_claim", agent="worker-1", kind="observation", statement="hex works",
             evidence=[[r["id"], "supports"]])
    b = call(mcp, "benchbook_brief", agent="worker-1", task_id=tid)
    assert "## Your task" in b and r["id"] in b
    call(mcp, "benchbook_complete_task", agent="worker-1", task_id=tid, status="done", summary="hex 2.5")
    d = call(mcp, "benchbook_decide", agent="lead", target_id=c["id"], new_status="supported", oracle="metric",
             rationale="measured", evidence_ids=[r["id"]])
    assert d["status"] == "supported"
    st = call(mcp, "benchbook_status", agent="lead")
    assert st["dedup"]["rejected"] == 1
    rows = call(mcp, "benchbook_sql", agent="lead", query="SELECT status FROM v_claim_status")
    assert rows["rows"] == [{"status": "supported"}]
    got = call(mcp, "benchbook_get", agent="worker-1", id=c["id"])
    assert got["links_in"][0]["src"] == r["id"]


def test_errors_are_actionable(project: str) -> None:
    mcp = build(project, "all")
    with pytest.raises(Exception) as ei:
        call(mcp, "benchbook_record_experiment", agent="worker-1", config={"a": 1}, metrics={"score": [1]},
             run_status="nope")
    msg = str(ei.value)
    assert "run_status" in msg and "metrics.score" in msg
    with pytest.raises(Exception, match="only orchestrator or human"):
        call(mcp, "benchbook_decide", agent="worker-9", target_id="c:00000000", new_status="supported",
             oracle="metric", rationale="x")


def test_check_tried_reports_invalidated_seeds(lead: Ledger, worker: Ledger) -> None:
    cfg = {"init": "hex"}
    result = worker.record_experiment(cfg, {"score": 2.5}, seed=0)
    assert result.id is not None
    lead.decide(result.id, "invalidated", "human", "broken evaluator")
    out = call(build(lead.project_id, "all"), "benchbook_check_tried", agent=worker.agent, config=cfg)
    assert out["tried"] and out["exact"] == [] and out["best_value"] is None
    assert out["invalidated"][0]["seed"] == 0 and "duplicate" in out["note"]


def test_mcp_uses_live_project_lease_default(lead: Ledger, worker: Ledger) -> None:
    mcp = build(lead.project_id, "all")
    call(mcp, "benchbook_status", agent=worker.agent)
    lead.create_tasks([{"title": "Try hex"}])
    lead.update_settings({"lease_seconds": 90})
    claim = call(mcp, "benchbook_next_task", agent=worker.agent)["task"]
    remaining = (datetime.fromisoformat(claim["lease_expires_at"]) - datetime.now(UTC)).total_seconds()
    assert 85 < remaining <= 90
    lead.update_settings({"lease_seconds": 180})
    beat = call(mcp, "benchbook_heartbeat", agent=worker.agent, task_id=claim["id"])
    remaining = (datetime.fromisoformat(beat["lease_expires_at"]) - datetime.now(UTC)).total_seconds()
    assert 175 < remaining <= 180


def test_internal_errors_carry_a_hint(project: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    def down(*a: Any, **k: Any) -> str:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(Ledger, "brief", down)
    with pytest.raises(Exception) as ei:
        call(build(project, "all"), "benchbook_brief", agent="worker-1")
    msg = str(ei.value)
    assert "OperationalError" in msg and "benchbook doctor" in msg and "not a problem with your input" in msg


def test_schema_resource(project: str) -> None:
    mcp = build(project, "all")
    res = asyncio.run(mcp.read_resource("benchbook://schema"))
    text = res[0].content if hasattr(res[0], "content") else str(res)
    assert "v_tried" in text and "columns:" in text


def test_stdio_server_discovers_tools_before_project_init(
    tmp_path: Path, db_url: str,
) -> None:
    async def check() -> None:
        params = StdioServerParameters(command=sys.executable, args=["-m", "benchbook.cli", "mcp"], cwd=tmp_path,
                                       env={"BENCHBOOK_DATABASE_URL": db_url})
        async with stdio_client(params) as (read, write), ClientSession(read, write, read_timeout_seconds=10) as client:
            await client.initialize()
            tools = await client.list_tools()
            assert "benchbook_create_tasks" in {tool.name for tool in tools.tools}
            before = await client.call_tool("benchbook_status", {"agent": "lead"})
            assert before.is_error and "benchbook init" in before.content[0].text

            project = make_project(db_url)
            write_config(tmp_path, project)
            result = await client.call_tool("benchbook_create_tasks",
                                            {"agent": "lead", "tasks": [{"title": "First task"}]})
            assert not result.is_error
            assert len(Ledger.open(project=project, agent="lead", database_url=db_url).list_tasks()) == 1

    asyncio.run(check())
