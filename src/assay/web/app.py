"""Read-only web viewer (§13) and the HTTP MCP endpoint. Starlette + Jinja2 + htmx, live updates via
LISTEN/NOTIFY -> Server-Sent Events. No build step, no CDN."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import psycopg
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ..db.conn import notification_channel
from ..errors import AssayError
from ..ids import short
from ..ledger import Ledger, config_line, jsonable

HERE = Path(__file__).parent
CLAIM_COLUMNS = ["proposed", "supported", "verified", "inconclusive", "refuted", "superseded", "retracted"]


def _age(t: Any, now: Any) -> str:
    from ..brief import age

    return age(now, t)


class NotifyHub:
    """One LISTEN connection per viewer, fanned out to every /events client, so browser tabs never use up the
    database's connections. It opens with the first subscriber and closes after the last one leaves."""

    QUEUE = 100

    def __init__(self, url: str, channel: str) -> None:
        self.url, self.channel = url, channel
        self.subscribers: set[asyncio.Queue[str]] = set()
        self.connects = 0
        self._task: asyncio.Task[None] | None = None

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(self.QUEUE)
        self.subscribers.add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen())
        try:
            yield queue
        finally:
            self.subscribers.discard(queue)
            if not self.subscribers and self._task is not None:
                self._task.cancel()
                self._task = None

    def _publish(self, payload: str) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # a slow client misses a refresh hint, not data: every page reloads from the ledger

    async def _listen(self) -> None:
        delay = 1.0
        while self.subscribers:
            try:
                aconn = await psycopg.AsyncConnection.connect(self.url, autocommit=True)
                self.connects += 1
                async with aconn:
                    await aconn.execute(psycopg.sql.SQL("LISTEN {}").format(psycopg.sql.Identifier(self.channel)))
                    delay = 1.0
                    async for n in aconn.notifies():
                        self._publish(n.payload)
            except psycopg.Error:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)


class Viewer:
    def __init__(self, lg: Ledger) -> None:
        self.lg = lg
        self.env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())
        self.env.globals.update(short=short, config_line=config_line, project=lg.project)
        self.env.filters["num"] = lambda v: "" if v is None else f"{v:.6g}"
        self.env.filters["js"] = lambda v: json.dumps(jsonable(v), ensure_ascii=False, indent=2, default=str)
        self.hub = NotifyHub(lg.url, notification_channel(lg.project_id))

    async def q(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        return await anyio.to_thread.run_sync(self.lg.sql_internal, query, params)

    def render(self, name: str, request: Request, **ctx: Any) -> HTMLResponse:
        partial = request.headers.get("HX-Request") == "true" and request.query_params.get("partial") == "1"
        tpl = self.env.get_template(name)
        parts = ctx.pop("parts", None)
        if parts is None and self.partitioned:
            parts = self._partition_list()
        cur = ctx.pop("cur", None) or self._cur(request, parts or [])
        return HTMLResponse(tpl.render(request=request, partial=partial, page=name.split(".")[0], parts=parts or [],
                                       cur=cur, partitioned=self.partitioned, **ctx))

    # ------------------------------------------------------------------ partitions (Step 3, §14)
    @property
    def partitioned(self) -> bool:
        return bool(self.lg.project.settings.get("partition_key"))

    def _partition_list(self) -> list[str]:
        rows = self.lg.sql_internal(
            "SELECT config->>%s AS p, count(*) AS n FROM assay.experiments WHERE project_id=%s"
            " AND config ? %s GROUP BY 1 ORDER BY 2 DESC, 1",
            (self.lg.project.settings["partition_key"], self.lg.project_id, self.lg.project.settings["partition_key"]))
        return [r["p"] for r in rows]

    @staticmethod
    def _cur(request: Request, parts: list[str]) -> str | None:
        p = request.query_params.get("p")
        return p if p in parts else (parts[0] if parts else None)

    async def partition_overview(self, request: Request) -> Response:
        from ..integrations.autoresearch.ops import status as ar_status

        parts = await anyio.to_thread.run_sync(self._partition_list)
        cur = self._cur(request, parts)
        st = await anyio.to_thread.run_sync(ar_status, self.lg, cur) if cur else {"partitions": []}
        ps = st["partitions"][0] if st["partitions"] else None
        now = (await self.q("SELECT now() AS t"))[0]["t"]
        activity = await self.q(
            "SELECT * FROM assay.v_activity WHERE project_id=%s AND kind <> 'task_event'"
            " ORDER BY created_at DESC LIMIT 25", (self.lg.project_id,))
        return self.render("ar_overview.html", request, ps=ps, parts=parts, cur=cur, activity=activity, now=now,
                           age=_age)

    async def partition_progress(self, request: Request) -> Response:
        cur = request.query_params.get("p")
        pid = self.lg.project_id
        verified = await self.q(
            "SELECT extract(epoch FROM created_at) AS t, mean FROM assay.v_partition_best WHERE project_id=%s"
            " AND partition=%s AND status='verified' ORDER BY created_at", (pid, cur))
        cands = await self.q(
            "SELECT extract(epoch FROM created_at) AS t, mean FROM assay.v_partition_best WHERE project_id=%s"
            " AND partition=%s AND status='candidate'", (pid, cur))
        refuted = await self.q(
            "SELECT extract(epoch FROM e.created_at) AS t, e.primary_value AS v FROM assay.claims c"
            " JOIN assay.v_claim_status s ON s.id = c.id"
            " JOIN assay.experiments e ON e.id = (c.payload->>'origin')::uuid"
            " WHERE c.project_id=%s AND c.kind='result' AND c.payload->>'partition'=%s"
            " AND s.status IN ('refuted','inconclusive') AND e.primary_value IS NOT NULL", (pid, cur))
        minimize = self.lg.project.metric_direction == "min"
        xs = sorted({float(r["t"]) for r in verified + cands + refuted})
        vmap = sorted((float(r["t"]), r["mean"]) for r in verified)
        cmap = {float(r["t"]): r["mean"] for r in cands}
        rmap = {float(r["t"]): r["v"] for r in refuted}
        step: list[float | None] = []
        best: float | None = None
        i = 0
        for x in xs:
            while i < len(vmap) and vmap[i][0] <= x:
                v = vmap[i][1]
                best = v if best is None or (v < best if minimize else v > best) else best
                i += 1
            step.append(best)
        return JSONResponse({"kind": "partition", "t": xs, "verified": step, "candidate": [cmap.get(x) for x in xs],
                             "refuted": [rmap.get(x) for x in xs]})

    async def lineage(self, request: Request) -> Response:
        parts = await anyio.to_thread.run_sync(self._partition_list)
        cur = self._cur(request, parts)
        pid = self.lg.project_id
        rows = await self.q("SELECT * FROM assay.v_lineage WHERE project_id=%s AND partition=%s"
                            " AND coalesce(seed,0)=0 ORDER BY created_at, id LIMIT 2000", (pid, cur))
        by_id = {r["id"]: r for r in rows}
        best = await self.q("SELECT origin_id, mean FROM assay.v_partition_best WHERE project_id=%s AND partition=%s"
                            " AND status='verified' AND rank=1", (pid, cur))
        path: list[dict[str, Any]] = []
        node = by_id.get(best[0]["origin_id"]) if best else None
        while node is not None and len(path) < 500:
            path.append(node)
            node = by_id.get(node["parent_id"])
        children: dict[Any, list[dict[str, Any]]] = {}
        for r in rows:
            children.setdefault(r["parent_id"] if r["parent_id"] in by_id else None, []).append(r)
        tree: list[dict[str, Any]] = []

        def walk(parent: Any, depth: int) -> None:
            for r in children.get(parent, []):
                tree.append({**r, "indent": depth})
                if depth < 200:
                    walk(r["id"], depth + 1)

        walk(None, 0)
        on_path = {r["id"] for r in path}
        return self.render("lineage.html", request, path=path, tree=tree, on_path=on_path, parts=parts, cur=cur,
                           best=best[0] if best else None)

    async def knobs(self, request: Request) -> Response:
        parts = await anyio.to_thread.run_sync(self._partition_list)
        cur = self._cur(request, parts)
        rows = await self.q("SELECT * FROM assay.v_knob_history WHERE project_id=%s AND partition=%s"
                            " ORDER BY knob, last_at DESC", (self.lg.project_id, cur))
        return self.render("knobs.html", request, rows=rows, parts=parts, cur=cur)

    def _diff(self, rec: dict[str, Any]) -> list[tuple[str, str]] | None:
        """Unified diff of an experiment's train.py blob against its derived_from parent (§14)."""
        import difflib

        if rec.get("kind") != "experiment":
            return None
        parent = next((lk["dst"] for lk in rec.get("links_out") or [] if lk["relation"] == "derived_from"), None)
        if parent is None:
            return None

        def blob(r: dict[str, Any]) -> str | None:
            for a in r.get("artifacts") or []:
                if isinstance(a, dict) and str(a.get("uri", "")).startswith("assay://blob/"):
                    try:
                        return self.lg.get_blob(a["uri"]).decode("utf-8", errors="replace")
                    except AssayError:
                        return None
            return None

        mine = blob(rec["record"])
        theirs = blob(self.lg.get(parent)["record"])
        if mine is None or theirs is None:
            return None
        lines = difflib.unified_diff(theirs.splitlines(), mine.splitlines(), f"{parent} train.py",
                                     f"{rec['id']} train.py", lineterm="", n=3)
        out = []
        for ln in lines:
            cls = "add" if ln.startswith("+") and not ln.startswith("+++") else (
                "del" if ln.startswith("-") and not ln.startswith("---") else ("hunk" if ln.startswith("@@") else ""))
            out.append((cls, ln))
        return out[:3000]

    # ------------------------------------------------------------------ pages
    async def overview(self, request: Request) -> Response:
        if self.partitioned:
            return await self.partition_overview(request)
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
            return PlainTextResponse(str(e), status_code=404)
        diff = await anyio.to_thread.run_sync(self._diff, rec)
        return self.render("record.html", request, r=rec, diff=diff)

    # ------------------------------------------------------------------ live updates
    async def events(self, request: Request) -> Response:
        async def stream() -> AsyncIterator[bytes]:
            yield b"retry: 3000\n\n"
            async with self.hub.subscribe() as queue:
                while not await request.is_disconnected():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=10.0)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield f"event: change\ndata: {payload}\n\n".encode()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def routes(self) -> list[Any]:
        return [
            Route("/", self.overview),
            Route("/experiments", self.experiments),
            Route("/claims", self.claims),
            Route("/tasks", self.tasks),
            Route("/r/{rid}", self.record),
            Route("/lineage", self.lineage),
            Route("/knobs", self.knobs),
            Route("/api/progress", self.progress),
            Route("/api/partition_progress", self.partition_progress),
            Route("/events", self.events),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        ]


LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
TOKEN_COOKIE = "assay_token"


def allowed_hosts(host: str) -> list[str]:
    """Host headers the viewer answers to: loopback names, plus the bound address when it is a specific one. Blocks
    DNS rebinding, where a web page's own hostname resolves to this machine."""
    hosts = list(LOCAL_HOSTS)
    if host not in WILDCARD_HOSTS:
        h = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if h.lower() not in hosts:
            hosts.append(h.lower())
    return hosts


class TokenMiddleware:
    """Required for a wildcard bind. Accepts `Authorization: Bearer <token>` (MCP clients), the cookie set after
    one visit to `/?token=<token>` (browsers), or that query parameter."""

    def __init__(self, app: Any, token: str) -> None:
        self.app, self.token = app, token

    def _ok(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate.encode(), self.token.encode())

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        req = Request(scope)
        auth = req.headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else None
        via_query = self._ok(req.query_params.get("token"))
        if not (via_query or self._ok(bearer) or self._ok(req.cookies.get(TOKEN_COOKIE))):
            await PlainTextResponse("assay: token required (open /?token=... once, or send Authorization: Bearer)",
                                    status_code=401)(scope, receive, send)
            return
        if not via_query:
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message: Any) -> None:
            if message["type"] == "http.response.start":
                cookie = f"{TOKEN_COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict".encode("latin-1")
                message = {**message, "headers": [*message.get("headers", []), (b"set-cookie", cookie)]}
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def create_app(project: str | None = None, toolset: str = "all", with_mcp: bool = True,
               host: str = "127.0.0.1", token: str | None = None) -> Starlette:
    """`host` is the bound address. A wildcard bind (0.0.0.0, ::) needs `token` (or ASSAY_SERVE_TOKEN): the MCP
    endpoint accepts unauthenticated writes and decisions, and the Host check cannot know the names to allow."""
    import os

    token = token or os.environ.get("ASSAY_SERVE_TOKEN") or None
    wildcard = host in WILDCARD_HOSTS
    if wildcard and not token:
        raise AssayError(f"binding to {host or 'all interfaces'} exposes unauthenticated ledger writes; "
                         "set ASSAY_SERVE_TOKEN (clients then send Authorization: Bearer <token>) "
                         "or bind to 127.0.0.1")
    lg = Ledger.open(project=project, agent="human")
    viewer = Viewer(lg)
    if with_mcp:
        from ..mcp_server import build

        app = build(project, toolset).streamable_http_app(host=host)
        app.router.routes.extend(viewer.routes())
    else:
        app = Starlette(routes=viewer.routes())
    if token:
        app.add_middleware(TokenMiddleware, token=token)
    # added last, so it runs first
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"] if wildcard else allowed_hosts(host))
    return app


def serve(host: str = "127.0.0.1", port: int = 7466, toolset: str = "all", project: str | None = None,
          token: str | None = None) -> None:
    import logging

    import uvicorn

    logging.getLogger("mcp").setLevel(logging.WARNING)
    app = create_app(project, toolset, host=host, token=token)
    print(f"assay viewer  http://{host}:{port}/\nassay MCP     http://{host}:{port}/mcp  (streamable HTTP)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
