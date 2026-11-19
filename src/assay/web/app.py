"""Read-only web viewer (§13) and the HTTP MCP endpoint. Starlette + Jinja2 + htmx, live updates via
LISTEN/NOTIFY -> Server-Sent Events. No build step, no CDN."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import psycopg
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..errors import AssayError
from ..ids import short
from ..ledger import Ledger, config_line, jsonable

HERE = Path(__file__).parent
CLAIM_COLUMNS = ["proposed", "supported", "verified", "inconclusive", "refuted", "superseded", "retracted"]


def _age(t: Any, now: Any) -> str:
    from ..brief import age

    return age(now, t)


class Viewer:
    def __init__(self, lg: Ledger) -> None:
        self.lg = lg
        self.env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())
        self.env.globals.update(short=short, config_line=config_line, project=lg.project)
        self.env.filters["num"] = lambda v: "" if v is None else f"{v:.6g}"
        self.env.filters["js"] = lambda v: json.dumps(jsonable(v), ensure_ascii=False, indent=2, default=str)

    async def q(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        return await anyio.to_thread.run_sync(self.lg.sql_internal, query, params)

    def render(self, name: str, request: Request, **ctx: Any) -> HTMLResponse:
        partial = request.headers.get("HX-Request") == "true" and request.query_params.get("partial") == "1"
        tpl = self.env.get_template(name)
        return HTMLResponse(tpl.render(request=request, partial=partial, page=name.split(".")[0], **ctx))

    # ------------------------------------------------------------------ pages
    async def overview(self, request: Request) -> Response:
        pid = self.lg.project_id
        st = await anyio.to_thread.run_sync(self.lg.status)
        now = (await self.q("SELECT now() AS t"))[0]["t"]
        activity = await self.q("SELECT * FROM assay.v_activity WHERE project_id=%s AND kind <> 'task_event'"
                                " ORDER BY created_at DESC LIMIT 25", (pid,))
        verified = st["claims"].get("verified", 0)
        return self.render("overview.html", request, st=st, activity=activity, verified=verified, now=now,
                           age=_age)

    async def progress(self, request: Request) -> Response:
        rows = await self.q("SELECT extract(epoch FROM created_at) AS t, primary_value, best_so_far"
                            " FROM assay.v_progress WHERE project_id=%s ORDER BY n", (self.lg.project_id,))
        return JSONResponse({"t": [float(r["t"]) for r in rows], "value": [r["primary_value"] for r in rows],
                             "best": [r["best_so_far"] for r in rows]})

    async def experiments(self, request: Request) -> Response:
        qp = request.query_params
        author, outcome, tag = qp.get("author") or None, qp.get("outcome") or None, qp.get("tag") or None
        rows = await self.q(
            "SELECT e.*, (inv.target_id IS NOT NULL) AS invalidated, t.tags FROM assay.experiments e"
            " LEFT JOIN (SELECT DISTINCT target_id FROM assay.decisions WHERE new_status='invalidated') inv"
            "   ON inv.target_id = e.id"
            " LEFT JOIN assay.tasks t ON t.id = e.task_id"
            " WHERE e.project_id=%(p)s AND (%(a)s::text IS NULL OR e.author=%(a)s)"
            " AND (%(o)s::text IS NULL OR e.outcome=%(o)s) AND (%(g)s::text IS NULL OR %(g)s = ANY(t.tags))"
            " ORDER BY e.created_at DESC LIMIT 300",
            {"p": self.lg.project_id, "a": author, "o": outcome, "g": tag})
        authors = await self.q("SELECT DISTINCT author FROM assay.experiments WHERE project_id=%s ORDER BY 1",
                               (self.lg.project_id,))
        tags = await self.q("SELECT DISTINCT unnest(tags) AS tag FROM assay.tasks WHERE project_id=%s ORDER BY 1",
                            (self.lg.project_id,))
        return self.render("experiments.html", request, rows=rows, authors=[a["author"] for a in authors],
                           tags=[t["tag"] for t in tags], f={"author": author, "outcome": outcome, "tag": tag})

    async def claims(self, request: Request) -> Response:
        rows = await self.q("SELECT * FROM assay.v_claim_status WHERE project_id=%s ORDER BY created_at DESC",
                            (self.lg.project_id,))
        cols = {c: [r for r in rows if r["status"] == c] for c in CLAIM_COLUMNS}
        return self.render("claims.html", request, cols=cols)

    async def tasks(self, request: Request) -> Response:
        rows = await self.q(
            "SELECT *, extract(epoch FROM lease_expires_at - now())::int AS lease_left FROM assay.tasks"
            " WHERE project_id=%s ORDER BY CASE status WHEN 'claimed' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,"
            " priority DESC, created_at", (self.lg.project_id,))
        return self.render("tasks.html", request, rows=rows)

    async def record(self, request: Request) -> Response:
        try:
            rec = await anyio.to_thread.run_sync(self.lg.get, request.path_params["rid"])
        except AssayError as e:
            return HTMLResponse(f"<p>{e}</p>", status_code=404)
        return self.render("record.html", request, r=rec)

    # ------------------------------------------------------------------ live updates
    async def events(self, request: Request) -> Response:
        channel = f"assay_{self.lg.project_id}"

        async def stream() -> AsyncIterator[bytes]:
            yield b"retry: 3000\n\n"
            try:
                aconn = await psycopg.AsyncConnection.connect(self.lg.url, autocommit=True)
            except psycopg.Error:
                return
            async with aconn:
                await aconn.execute(psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(channel)))
                while not await request.is_disconnected():
                    got = False
                    async for n in aconn.notifies(timeout=10.0):
                        got = True
                        yield f"event: change\ndata: {n.payload}\n\n".encode()
                        # drain bursts quickly; the client debounces refreshes
                        await asyncio.sleep(0)
                    if not got:
                        yield b": keepalive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def routes(self) -> list[Any]:
        return [
            Route("/", self.overview),
            Route("/experiments", self.experiments),
            Route("/claims", self.claims),
            Route("/tasks", self.tasks),
            Route("/r/{rid}", self.record),
            Route("/api/progress", self.progress),
            Route("/events", self.events),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ]


def create_app(project: str | None = None, toolset: str = "all", with_mcp: bool = True,
               host: str = "127.0.0.1") -> Starlette:
    lg = Ledger.open(project=project, agent="human")
    viewer = Viewer(lg)
    if with_mcp:
        from ..mcp_server import build

        app = build(project, toolset).streamable_http_app(host=host)
        app.router.routes.extend(viewer.routes())
        return app
    return Starlette(routes=viewer.routes())


def serve(host: str = "127.0.0.1", port: int = 7466, toolset: str = "all", project: str | None = None) -> None:
    import logging

    import uvicorn

    logging.getLogger("mcp").setLevel(logging.WARNING)
    app = create_app(project, toolset, host=host)
    print(f"assay viewer  http://{host}:{port}/\nassay MCP     http://{host}:{port}/mcp  (streamable HTTP)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
