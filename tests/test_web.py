"""Viewer pages render with live data; the HTTP MCP endpoint is mounted."""
from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from benchbook import Ledger
from benchbook.db.conn import notification_channel
from benchbook.errors import BenchbookError
from benchbook.web.app import NotifyHub, allowed_hosts, create_app

MCP_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


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
        bad_record = client.get("/r/invalid&record")
        assert bad_record.status_code == 404
        assert bad_record.headers["content-type"] == "text/plain; charset=utf-8"
        assert "invalid&record" in bad_record.text
        part = client.get("/claims?partial=1", headers={"HX-Request": "true"})
        assert "<html" not in part.text and 'id="content"' in part.text
        assert client.get("/static/htmx.min.js").status_code == 200
        init = client.post("/mcp", json=MCP_INIT, headers=MCP_HEADERS)
        assert init.status_code == 200 and "benchbook" in init.text


def test_host_header_is_checked(lead: Ledger) -> None:
    app = create_app(lead.project_id, with_mcp=True)
    with TestClient(app, base_url="http://localhost:7466") as client:
        assert client.get("/tasks").status_code == 200
        # DNS rebinding: a page on attacker.example resolving to 127.0.0.1 sends its own Host header
        assert client.get("/tasks", headers={"Host": "attacker.example"}).status_code == 400
        assert client.get("/events", headers={"Host": "attacker.example:7466"}).status_code == 400
    assert allowed_hosts("127.0.0.1") == ["localhost", "127.0.0.1", "[::1]"]
    assert allowed_hosts("10.0.0.5")[-1] == "10.0.0.5" and allowed_hosts("fe80::1")[-1] == "[fe80::1]"


@pytest.mark.parametrize(("host", "authority"), [
    ("0.0.0.0", "lab-box"), ("::", "[fd00::1]"), ("10.0.0.5", "10.0.0.5"),
    ("fd00::1", "[fd00::1]"), ("lab-box", "lab-box"),
])
def test_non_loopback_bind_needs_a_token(lead: Ledger, monkeypatch: pytest.MonkeyPatch,
                                        host: str, authority: str) -> None:
    monkeypatch.delenv("BENCHBOOK_SERVE_TOKEN", raising=False)
    with pytest.raises(BenchbookError, match="BENCHBOOK_SERVE_TOKEN"):
        create_app(lead.project_id, host=host)
    app = create_app(lead.project_id, host=host, token="s3cret")
    with TestClient(app, base_url=f"http://{authority}:7466") as client:
        assert client.get("/tasks").status_code == 401
        assert client.get("/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/tasks", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.post("/mcp", json=MCP_INIT, headers=MCP_HEADERS).status_code == 401
        init = client.post("/mcp", json=MCP_INIT, headers={**MCP_HEADERS, "Authorization": "Bearer s3cret"})
        assert init.status_code == 200
        first = client.get("/?token=s3cret", follow_redirects=False)
        assert first.status_code == 303 and "benchbook_token" in first.headers["set-cookie"]
        assert first.headers["location"] == "/"
        assert "s3cret" not in client.get(first.headers["location"]).text
        assert client.get("/tasks").status_code == 200  # the cookie now carries it


@pytest.mark.parametrize(("host", "authority"), [("127.0.0.2", "127.0.0.2"), ("::1", "[::1]")])
def test_loopback_bind_allows_its_host_header(lead: Ledger, host: str, authority: str) -> None:
    app = create_app(lead.project_id, host=host)
    with TestClient(app, base_url=f"http://{authority}:7466") as client:
        assert client.get("/tasks").status_code == 200
        assert client.post("/mcp", json=MCP_INIT, headers=MCP_HEADERS).status_code == 200


def test_event_clients_share_one_listen_connection(lead: Ledger) -> None:
    hub = NotifyHub(lead.url, notification_channel(lead.project_id))

    async def run() -> None:
        async with hub.subscribe() as a, hub.subscribe() as b:
            await asyncio.sleep(1.0)  # let the listener connect
            await asyncio.to_thread(lead.create_tasks, [{"title": "ping"}])
            got = await asyncio.wait_for(asyncio.gather(a.get(), b.get()), timeout=10)
            assert all(got)
        assert hub._task is None and not hub.subscribers

    asyncio.run(run())
    assert hub.connects == 1
