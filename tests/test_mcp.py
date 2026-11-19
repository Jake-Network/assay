"""MCP adapter: toolsets, round trip, error messages, schema resource."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from assay.mcp_server import build, tool_names


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
    assert len(worker) == 9 and "assay_decide" not in worker and "assay_create_tasks" not in worker
    full = tool_names(build(project, "all"))
    assert {"assay_decide", "assay_create_tasks", "assay_cancel_task", "assay_status", "assay_sql",
            "assay_search"} <= set(full)


def test_round_trip(project: str) -> None:
    mcp = build(project, "all")
    out = call(mcp, "assay_create_tasks", agent="lead", tasks=[{"title": "hex", "tags": ["init"]}])
    tid = out["ids"][0]
    t = call(mcp, "assay_next_task", agent="worker-1")
    assert t["task"]["id"] == tid
    assert call(mcp, "assay_check_tried", agent="worker-1", config={"init": "hex"})["tried"] is False
    r = call(mcp, "assay_record_experiment", agent="worker-1", config={"init": "hex"}, metrics={"score": 2.5},
             seed=0, task_id=tid)
    assert r["outcome"] == "improved" and r["id"].startswith("e:")
    dup = call(mcp, "assay_record_experiment", agent="worker-1", config={"init": "hex"}, metrics={"score": 2.5},
               seed=0)
    assert dup["duplicate_of"] == r["id"]
    c = call(mcp, "assay_propose_claim", agent="worker-1", kind="observation", statement="hex works",
             evidence=[[r["id"], "supports"]])
    b = call(mcp, "assay_brief", agent="worker-1", task_id=tid)
    assert "## Your task" in b and r["id"] in b
    call(mcp, "assay_complete_task", agent="worker-1", task_id=tid, status="done", summary="hex 2.5")
    d = call(mcp, "assay_decide", agent="lead", target_id=c["id"], new_status="supported", oracle="metric",
             rationale="measured", evidence_ids=[r["id"]])
    assert d["status"] == "supported"
    st = call(mcp, "assay_status", agent="lead")
    assert st["dedup"]["rejected"] == 1
    rows = call(mcp, "assay_sql", agent="lead", query="SELECT status FROM v_claim_status")
    assert rows["rows"] == [{"status": "supported"}]
    got = call(mcp, "assay_get", agent="worker-1", id=c["id"])
    assert got["links_in"][0]["src"] == r["id"]


def test_errors_are_actionable(project: str) -> None:
    mcp = build(project, "all")
    with pytest.raises(Exception) as ei:
        call(mcp, "assay_record_experiment", agent="worker-1", config={"a": 1}, metrics={"score": [1]},
             run_status="nope")
    msg = str(ei.value)
    assert "run_status" in msg and "metrics.score" in msg
    with pytest.raises(Exception, match="only orchestrator or human"):
        call(mcp, "assay_decide", agent="worker-9", target_id="c:00000000", new_status="supported",
             oracle="metric", rationale="x")


def test_schema_resource(project: str) -> None:
    mcp = build(project, "all")
    res = asyncio.run(mcp.read_resource("assay://schema"))
    text = res[0].content if hasattr(res[0], "content") else str(res)
    assert "v_tried" in text and "columns:" in text
