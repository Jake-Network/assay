"""Viewer pages render with live data; the HTTP MCP endpoint is mounted."""
from __future__ import annotations

from starlette.testclient import TestClient

from assay import Ledger
from assay.web.app import create_app


def test_pages(lead: Ledger, worker: Ledger) -> None:
    [tid] = lead.create_tasks([{"title": "Try hex", "tags": ["init"]}])
    worker.next_task()
    e = worker.record_experiment({"init": "hex"}, {"score": 2.5}, task_id=tid, notes="good")
    c = worker.propose_claim("hypothesis", "hex wins", evidence=[(e.id, "supports")])  # type: ignore[list-item]
    lead.decide(c.id, "supported", "metric", "measured", [e.id])  # type: ignore[list-item]
    app = create_app(lead.project_id, with_mcp=True)
    with TestClient(app, base_url="http://127.0.0.1:7466") as client:  # MCP checks the Host header
        r = client.get("/")
        assert r.status_code == 200 and "2.5" in r.text and "best-so-far" in r.text
        assert client.get("/api/progress").json()["best"] == [2.5]
        r = client.get("/experiments?outcome=improved")
        assert r.status_code == 200 and f"e:{str(e.id)[:8]}" in r.text
        assert "hex wins" in client.get("/claims").text
        assert "Try hex" in client.get("/tasks").text
        r = client.get(f"/r/c:{str(c.id)[:8]}")
        assert r.status_code == 200 and "supports" in r.text
        assert client.get("/r/e:zzzzzzzz").status_code == 404
        part = client.get("/claims?partial=1", headers={"HX-Request": "true"})
        assert "<html" not in part.text and 'id="content"' in part.text
        assert client.get("/static/htmx.min.js").status_code == 200
        init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            headers={"Accept": "application/json, text/event-stream"})
        assert init.status_code == 200 and "assay" in init.text
