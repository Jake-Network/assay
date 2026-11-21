"""`benchbook` CLI: a thin adapter over the SDK (P10). --json anywhere prints one JSON value.
Exit codes: 0 ok, 1 user error, 2 duplicate rejected (exp record), 3 no claimable task."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from .agent_cli import agent_app
from .check_cli import check_app
from .config import env
from .errors import BenchbookError, Conflict, ValidationFailed
from .finding_cli import finding_app
from .ids import short
from .piece_cli import piece_app

if TYPE_CHECKING:
    from .ledger import Ledger

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="benchbook: your agents' lab notebook. Nothing they find gets lost, repeated, "
                       "or trusted without evidence.")
task_app = typer.Typer(no_args_is_help=True, help="Tasks: add, claim, finish.")
exp_app = typer.Typer(no_args_is_help=True, help="Experiments: record, check whether tried.")
claim_app = typer.Typer(no_args_is_help=True, help="Claims: propose, list, show.")
db_app = typer.Typer(no_args_is_help=True, help="Local embedded Postgres.")
app.add_typer(task_app, name="task")
app.add_typer(exp_app, name="exp")
app.add_typer(claim_app, name="claim")
app.add_typer(db_app, name="db")
app.add_typer(agent_app, name="agent")
app.add_typer(check_app, name="check")
app.add_typer(piece_app, name="piece")
app.add_typer(finding_app, name="finding")

STATE: dict[str, Any] = {"json": False, "project": None, "agent": None}


def _print_version(value: bool) -> None:
    if value:
        from . import __version__

        print(f"benchbook {__version__}")
        raise typer.Exit()


@app.callback()
def _root(version: Annotated[bool, typer.Option("--version", "-V", callback=_print_version, is_eager=True,
                                                help="Print the installed build (version and commit) and exit.")]
          = False) -> None:
    pass


# ------------------------------------------------------------------ helpers

def die(msg: str, code: int = 1) -> NoReturn:
    if STATE["json"]:
        print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    else:
        print(f"error: {msg}", file=sys.stderr)
    raise typer.Exit(code)


def emit(data: Any, text: str | None = None) -> None:
    from .ledger import jsonable

    if STATE["json"]:
        print(json.dumps(jsonable(data), ensure_ascii=False, default=str))
    else:
        print(text if text is not None else json.dumps(jsonable(data), ensure_ascii=False, indent=2, default=str))


def ledger(agent: str | None = None, *, read_only: bool = False) -> Ledger:
    """Writes need a name: --agent, BENCHBOOK_AGENT or (for decide) --as. Reads that write nothing fall back to
    'human' so a person can look around without naming themselves (I1). With BENCHBOOK_TOKEN the token names the
    agent, so there is no fallback."""
    from .config import default_agent
    from .ledger import Ledger

    fallback = default_agent("human") if read_only else None
    name = agent or STATE["agent"] or env("AGENT") or fallback
    return Ledger.open(project=STATE["project"], agent=name)


def load_json(value: str | None, what: str) -> Any:
    """'@path' reads a file, '-' reads stdin, anything else is parsed as JSON."""
    if value is None:
        return None
    raw = value
    if value == "-":
        raw = sys.stdin.read()
    elif value.startswith("@"):
        p = Path(value[1:])
        if not p.is_file():
            die(f"{what}: file not found: {p}")
        raw = p.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"{what}: invalid JSON ({e})")


def csv(value: str | None) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()] if value else []


def task_line(t: Any) -> str:
    lease = f" lease={t.lease_owner}" if t.status == "claimed" else ""
    tags = f" [{', '.join(t.tags)}]" if t.tags else ""
    return f"{t.short_id}  {t.status:<9} p{t.priority}{tags}{lease}  {t.title}"


# ------------------------------------------------------------------ project

@app.command()
def init(
    project: Annotated[str | None, typer.Option(help="Project slug (default: directory name)")] = None,
    title: Annotated[str | None, typer.Option()] = None,
    goal: Annotated[str | None, typer.Option(help="One sentence; shown at the top of every brief")] = None,
    metric: Annotated[str | None, typer.Option(help="Primary metric key, e.g. sum_radii")] = None,
    direction: Annotated[str | None, typer.Option(help="max or min")] = None,
    epsilon: Annotated[float, typer.Option(help="Minimum change that counts as improved")] = 0.0,
    config_schema: Annotated[str | None, typer.Option("--config-schema",
                                                      help="JSON Schema for experiment configs (@file)")] = None,
    pack: Annotated[str, typer.Option(help="core, ml-research or code (coding pieces and their checks)")] = "core",
) -> None:
    """Create a project and write .benchbook/config.toml in the repository root."""
    import re

    root = _repo_root()
    pid = project or STATE["project"] or re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-") or "benchbook"
    schema = load_json(config_schema, "--config-schema")
    from .ledger import Ledger
    from .packs import load_pack

    defaults = load_pack(pack).project_defaults  # e.g. code: checks_passed (max), partitioned by piece
    if metric is None and direction is None:
        metric, direction = defaults.get("metric"), defaults.get("direction")
    p, created = Ledger.init_project(pid, title=title, goal=goal or "", metric=metric, direction=direction,
                                     epsilon=epsilon, config_schema=schema, pack=pack,
                                     settings=dict(defaults.get("settings", {})) or None)
    from .config import write_config

    cfg = write_config(root, p.id)
    emit({"ok": True, "project": p.id, "created": created, "config": str(cfg), "pack": p.pack},
         f"{'created' if created else 'using existing'} project {p.id} (pack {p.pack})\n"
         f"  goal:   {p.goal}\n  metric: {p.primary_metric or '(none)'} {p.metric_direction or ''}\n"
         f"  config: {cfg}\nagents: lead (orchestrator), human (human), hook (harness)\n"
         f"next: BENCHBOOK_AGENT=me benchbook exp record --config '{{\"x\":1}}' --metrics '{{...}}'   |   benchbook ui")


def _repo_root() -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return Path.cwd()


@app.command()
def status() -> None:
    """One-screen project summary."""
    s = ledger(read_only=True).status()
    if STATE["json"]:
        return emit(s)
    b = s["best"]
    lines = [f"{s['project']}: {s['goal']}",
             f"best      {b['value']} ({b['id']}, {b['author']})" if b else "best      none yet",
             f"exps      {s['experiments']['total']} total, {s['experiments']['ok']} ok, "
             f"{s['experiments']['improved']} improved, {s['experiments']['invalidated']} invalidated",
             f"dedup     {s['dedup']['rejected']} rejected duplicates, {s['dedup']['avoided']} repeat-check matches",
             "claims    " + (", ".join(f"{k} {v}" for k, v in sorted(s["claims"].items())) or "none"),
             "tasks     " + ", ".join(f"{k} {v}" for k, v in sorted(s["tasks"].items())),
             "active    " + (", ".join(s["active_agents"]) or "none in the last 15 min")]
    print("\n".join(lines))


@app.command()
def brief(task: Annotated[str | None, typer.Option("--task")] = None,
          budget: Annotated[int | None, typer.Option("--budget")] = None) -> None:
    """Print the context brief (markdown) for this agent."""
    from .config import load

    b = ledger().brief(task_id=task, budget_tokens=load().default_budget_tokens if budget is None else budget)
    emit({"brief": b}, b.rstrip("\n"))


@app.command()
def get(id: str) -> None:
    """Show one record with its links."""
    emit(ledger(read_only=True).get(id))


@app.command()
def search(text: Annotated[str | None, typer.Argument()] = None,
           kind: Annotated[str | None, typer.Option()] = None,
           status: Annotated[str | None, typer.Option()] = None,
           tags: Annotated[str | None, typer.Option(help="records with ANY of these tags (comma separated)")] = None,
           all_tags: Annotated[str | None, typer.Option("--all-tags",
                                                        help="records with ALL of these tags (comma separated)")]
           = None,
           limit: Annotated[int, typer.Option()] = 20) -> None:
    """One-line summaries of claims, experiments and tasks, each ending with its tags."""
    rows = ledger(read_only=True).search(text, kind, status, csv(tags), limit, all_tags=csv(all_tags))
    emit(rows, "\n".join(f"{r['id']}  {r['line']}" for r in rows) or "(nothing)")


# ------------------------------------------------------------------ tasks

@task_app.command("add")
def task_add(title: str,
             spec: Annotated[str | None, typer.Option(help="JSON or @file")] = None,
             tags: Annotated[str | None, typer.Option(help="comma separated")] = None,
             priority: Annotated[int, typer.Option()] = 0,
             depends_on: Annotated[str | None, typer.Option("--depends-on")] = None,
             max_attempts: Annotated[int, typer.Option("--max-attempts")] = 3) -> None:
    """Create a task (orchestrator or human)."""
    ids = ledger().create_tasks([{"title": title, "spec": load_json(spec, "--spec") or {}, "tags": csv(tags),
                                  "priority": priority, "depends_on": csv(depends_on),
                                  "max_attempts": max_attempts}])
    emit({"ok": True, "ids": [short("task", i) for i in ids]}, short("task", ids[0]))


@task_app.command("next")
def task_next(tags: Annotated[str | None, typer.Option()] = None,
              lease: Annotated[int | None, typer.Option(help="lease seconds (default: settings.lease_seconds or 900)")]
              = None) -> None:
    """Claim the next task. Exit 3 when nothing is claimable."""
    t = ledger().next_task(tags=csv(tags), lease_seconds=lease)
    if t is None:
        emit({"ok": True, "task": None}, "no claimable task")
        raise typer.Exit(3)
    emit({"ok": True, "task": t.model_dump()}, task_line(t))


@task_app.command("claim")
def task_claim(id: str,
               lease: Annotated[int | None, typer.Option(help="lease seconds (default: settings.lease_seconds or 900)")]
               = None) -> None:
    """Claim one task by id (the lead assigned it to you). Exit 3 when it is not claimable."""
    try:
        t = ledger().claim_task(id, lease_seconds=lease)
    except Conflict as e:
        die(str(e), 3)
    emit({"ok": True, "task": t.model_dump()}, task_line(t))


@task_app.command("heartbeat")
def task_heartbeat(id: str, lease: Annotated[int | None, typer.Option()] = None) -> None:
    exp = ledger().heartbeat(id, lease)
    emit({"ok": True, "lease_expires_at": exp}, f"lease until {exp:%H:%M:%S}")


def _finish(id: str, status: str, summary: str) -> None:
    t = ledger().complete_task(id, status, summary)
    emit({"ok": True, "task": t.model_dump()}, task_line(t))


@task_app.command("done")
def task_done(id: str, summary: Annotated[str, typer.Option("--summary", "-m")] = "") -> None:
    _finish(id, "done", summary)


@task_app.command("fail")
def task_fail(id: str, summary: Annotated[str, typer.Option("--summary", "-m")] = "") -> None:
    _finish(id, "failed", summary)


@task_app.command("cancel")
def task_cancel(id: str, reason: Annotated[str, typer.Option()] = "cancelled") -> None:
    t = ledger().cancel_task(id, reason)
    emit({"ok": True, "task": t.model_dump()}, task_line(t))


@task_app.command("list")
def task_list(status: Annotated[str | None, typer.Option()] = None,
              limit: Annotated[int, typer.Option()] = 100) -> None:
    ts = ledger(read_only=True).list_tasks(status, limit)
    emit([t.model_dump() for t in ts], "\n".join(task_line(t) for t in ts) or "(no tasks)")


@task_app.command("show")
def task_show(id: str) -> None:
    emit(ledger(read_only=True).get(id))


# ------------------------------------------------------------------ experiments

@exp_app.command("record")
def exp_record(config: Annotated[str, typer.Option(help="JSON or @file")],
               metrics: Annotated[str | None, typer.Option(help="JSON or @file")] = None,
               run_status: Annotated[str, typer.Option("--run-status")] = "ok",
               seed: Annotated[int | None, typer.Option()] = None,
               code_ref: Annotated[str | None, typer.Option("--code-ref")] = None,
               notes: Annotated[str | None, typer.Option()] = None,
               task: Annotated[str | None, typer.Option()] = None,
               artifacts: Annotated[str | None, typer.Option(help="JSON list or @file")] = None,
               trace_ref: Annotated[str | None, typer.Option("--trace-ref")] = None,
               duration_ms: Annotated[int | None, typer.Option("--duration-ms")] = None,
               tokens: Annotated[int | None, typer.Option()] = None,
               cost_usd: Annotated[float | None, typer.Option("--cost-usd")] = None) -> None:
    """Record an evaluation (failures too). Exit 2 when it duplicates an existing ok run; a pack with
    dedup_mode = "notify" records it instead and names the earlier run in duplicate_of. Without --task the
    experiment belongs to the one task you hold a lease on, if there is exactly one."""
    lg = ledger()
    if task is None:
        leases = lg.live_leases()
        if len(leases) > 1:
            print(f"warning: you hold {len(leases)} tasks ({', '.join(t.short_id for t in leases)}); pass --task so "
                  "this experiment shows up in the briefs of related tasks", file=sys.stderr)
    r = lg.record_experiment(
        load_json(config, "--config"), load_json(metrics, "--metrics") or {}, run_status=run_status, seed=seed,
        code_ref=code_ref, notes=notes, task_id=task, artifacts=load_json(artifacts, "--artifacts"),
        trace_ref=load_json(trace_ref, "--trace-ref"), duration_ms=duration_ms, tokens=tokens, cost_usd=cost_usd)
    dup = short("experiment", r.duplicate_of) if r.duplicate_of is not None else None
    if r.id is None:  # refused (dedup_mode "refuse")
        emit({"ok": False, "duplicate_of": dup, "dedup_key": r.dedup_key},
             f"duplicate of {dup}: same config, code_ref and seed already "
             "recorded. Use a different seed to replicate.")
        raise typer.Exit(2)
    out = {"ok": True, "id": short("experiment", r.id), "outcome": r.outcome, "is_new_best": r.is_new_best,
           "duplicate_of": dup}
    if r.notice is not None:  # recorded repeat (dedup_mode "notify")
        out["notice"] = r.notice
        if not STATE["json"]:
            # stderr: the stdout line stays the one the capture hook reads as a write receipt
            print(f"notice: {r.notice}", file=sys.stderr)
    emit(out, f"{short('experiment', r.id)} {r.outcome}{'  ★ new best' if r.is_new_best else ''}")


@exp_app.command("tried")
def exp_tried(config: Annotated[str, typer.Option(help="JSON or @file")],
              code_ref: Annotated[str | None, typer.Option("--code-ref")] = None) -> None:
    """Has this exact config (and code_ref) been evaluated already?"""
    t = ledger().check_tried(load_json(config, "--config"), code_ref)
    data: dict[str, Any] = {"tried": bool(t.exact), "replicates": t.replicates, "best_value": t.best_value,
            "exact": [{"id": short("experiment", e.id), "author": e.author, "seed": e.seed,
                       "outcome": e.outcome, "value": e.primary_value, "notes": e.notes} for e in t.exact],
            "invalidated": [{"id": short("experiment", e.id), "seed": e.seed} for e in t.invalidated],
            "note": t.note}
    txt = ("not tried yet" if not t.exact else
           f"tried {len(t.exact)}x ({t.replicates} ok seeds), best {t.best_value}\n"
           + "\n".join(f"  {x['id']} seed={x['seed']} {x['outcome']} {x['value']} {x['notes'] or ''}"
                       for x in data["exact"]))
    if t.note:
        txt += f"\nnote: {t.note}"
    emit(data, txt)


def _register_exp_run() -> None:  # check, run and record in one command; it lives in exp_run.py
    from .exp_run import exp_run

    exp_app.command("run")(exp_run)


_register_exp_run()


# ------------------------------------------------------------------ claims, links, decisions

@claim_app.command("add")
def claim_add(kind: str, statement: str,
              supports: Annotated[str | None, typer.Option(help="ids this claim is supported by")] = None,
              refutes: Annotated[str | None, typer.Option(help="ids that refute this claim")] = None,
              tags: Annotated[str | None, typer.Option()] = None,
              payload: Annotated[str | None, typer.Option()] = None,
              task: Annotated[str | None, typer.Option()] = None) -> None:
    """Propose a claim (hypothesis|observation|conclusion|note)."""
    ev = [(i, "supports") for i in csv(supports)] + [(i, "refutes") for i in csv(refutes)]
    r = ledger().propose_claim(kind, statement, payload=load_json(payload, "--payload"), tags=csv(tags),
                               task_id=task, evidence=ev)
    cid = short("claim", r.id)
    emit({"ok": True, "id": cid, "duplicate_of": short("claim", r.duplicate_of) if r.duplicate_of else None,
          "links": [short("link", x) for x in r.link_ids]},
         f"{cid}" + (" (duplicate statement; evidence attached to the existing claim)" if r.duplicate_of else ""))


@claim_app.command("list")
def claim_list(status: Annotated[str | None, typer.Option()] = None,
               kind: Annotated[str | None, typer.Option()] = None,
               limit: Annotated[int, typer.Option()] = 50) -> None:
    rows = ledger(read_only=True).search(None, kind or "claim", status, None, limit)
    emit(rows, "\n".join(f"{r['id']}  {r['line']}" for r in rows) or "(no claims)")


@claim_app.command("show")
def claim_show(id: str) -> None:
    emit(ledger(read_only=True).get(id))


@app.command()
def link(src: str, relation: str, dst: str, note: Annotated[str | None, typer.Option()] = None) -> None:
    """Link two records: SRC supports|refutes|derived_from|depends_on|supersedes|replicates|cites DST."""
    lid = ledger().link(src, relation, dst, note)
    emit({"ok": True, "id": short("link", lid)}, short("link", lid))


@app.command()
def decide(target: str, status: str,
           oracle: Annotated[str, typer.Option(help="metric|rerun|pytest|lean|human|llm_judge|none")],
           rationale: Annotated[str, typer.Option()],
           evidence: Annotated[str | None, typer.Option(help="comma separated ids")] = None,
           as_: Annotated[str | None, typer.Option("--as", help="agent to decide as (e.g. human)")] = None,
           force: Annotated[bool, typer.Option(help="allow transitions outside the state graph")] = False,
           oracle_detail: Annotated[str | None, typer.Option("--oracle-detail")] = None) -> None:
    """Change a claim's status, or invalidate an experiment (orchestrator or human only)."""
    d = ledger(as_).decide(target, status, oracle, rationale, csv(evidence),
                           oracle_detail=load_json(oracle_detail, "--oracle-detail"), force=force)
    emit({"ok": True, "id": short("decision", d.id), "status": d.new_status},
         f"{short('decision', d.id)} {short(d.target_type, d.target_id)} -> {d.new_status}")


@app.command("sql")
def sql_cmd(query: str) -> None:
    """Read-only SQL over the v_* views of this project."""
    r = ledger(read_only=True).sql(query)
    if STATE["json"]:
        return emit(r)
    cols = r["columns"]
    print("\t".join(cols))
    for row in r["rows"]:
        print("\t".join("" if row[c] is None else str(row[c]) for c in cols))
    if r["truncated"]:
        print(f"(truncated: {r['returned']} of {r['row_count']} rows)", file=sys.stderr)


@app.command()
def export(fmt: Annotated[str, typer.Option("--format")] = "jsonl") -> None:
    """Export the project as an append-only JSONL event log."""
    if fmt != "jsonl":
        die("only --format jsonl is supported")
    for ev in ledger(read_only=True).export():
        print(json.dumps(ev, ensure_ascii=False, default=str))


# ------------------------------------------------------------------ servers and integration

@app.command()
def serve(host: Annotated[str, typer.Option()] = "127.0.0.1",
          port: Annotated[int, typer.Option()] = 7466,
          toolset: Annotated[str, typer.Option()] = "all",
          token: Annotated[str | None, typer.Option(help="required for non-loopback binds (or BENCHBOOK_SERVE_TOKEN)")]
          = None) -> None:
    """HTTP: streamable MCP at /mcp and the read-only viewer at /."""
    from .web.app import requires_serve_token
    from .web.app import serve as run

    if requires_serve_token(host):
        print(f"WARNING: binding {host}. Requests need the serve token (Bearer header, cookie or ?token=); anyone "
              "holding it can read and write the ledger. Use TLS or a private network.", file=sys.stderr)
    run(host=host, port=port, toolset=toolset, project=STATE["project"], token=token)


@app.command()
def mcp(toolset: Annotated[str, typer.Option(help="worker|orchestrator|all")] = "all") -> None:
    """stdio MCP server (Claude Code starts this)."""
    from .mcp_server import run_stdio

    run_stdio(toolset=toolset, project=STATE["project"])


@app.command()
def ui(port: Annotated[int, typer.Option()] = 7466) -> None:
    """Open the viewer in a browser (start `benchbook serve` first)."""
    import urllib.request
    import webbrowser

    url = f"http://127.0.0.1:{port}/"
    try:
        urllib.request.urlopen(url, timeout=1)
    except OSError:
        print(f"nothing is listening on {url}; run `benchbook serve` in another terminal", file=sys.stderr)
    webbrowser.open(url)
    emit({"url": url}, url)


@app.command()
def hook(event: str) -> None:
    """Claude Code hook entry point. Always exits 0, except `replay`, which reports what it processed."""
    from .hooks import main as hook_main

    res = hook_main(event)
    if res is not None:
        emit(res.as_dict(), str(res))
        if not res.ok:
            raise typer.Exit(1)


@app.command()
def migrate() -> None:
    """Apply schema migrations (Alembic)."""
    from .db.conn import database_url
    from .db.migrate import LEGACY_SCHEMA, status
    from .db.migrate import migrate as run

    url = database_url()
    renamed = {"renamed_from": LEGACY_SCHEMA} if status(url).legacy else {}  # a database from before the rename
    before, after = run(url)
    emit({"ok": True, "from": before, "to": after, **renamed},
         f"schema {before or 'empty'} -> {after}" + (f" (renamed from {LEGACY_SCHEMA})" if renamed else ""))


@app.command()
def doctor() -> None:
    """Check the installed build, database, schema and the Claude Code connection (PATH, plugin, env, hook)."""
    import platform

    from . import __version__
    from .doctor import Check, claude_code_checks, legacy_checks

    checks = [Check("benchbook build", True, __version__), *_db_checks()]
    checks.append(Check("python", sys.version_info >= (3, 11), platform.python_version()))
    checks += claude_code_checks(_repo_root())
    checks += legacy_checks()
    _report(checks)


def _db_checks() -> list[Any]:
    from .db.conn import data_dir, database_url
    from .db.migrate import LEGACY_SCHEMA, head, status
    from .doctor import Check

    checks: list[Check] = []
    try:
        mode = "url" if env("DATABASE_URL") else f"embedded ({data_dir()})"
    except RuntimeError as e:  # BENCHBOOK_DATA_DIR=~name of a user that does not exist
        return [Check("connect", False, f"BENCHBOOK_DATA_DIR: {e}",
                      "export BENCHBOOK_DATA_DIR=<an existing directory>"),
                Check("db schema", False, "unknown (no connection)"), Check("build schema head", True, head())]
    build_head = Check("build schema head", True, head())
    try:
        url = database_url()
        checks.append(Check("connect", True, mode))
    except Exception as e:  # noqa: BLE001
        checks.append(Check("connect", False, f"{mode}: {e}", "check BENCHBOOK_DATABASE_URL / BENCHBOOK_DATA_DIR"))
        return [*checks, Check("db schema", False, "unknown (no connection)"), build_head]
    import psycopg

    try:
        with psycopg.connect(url, connect_timeout=10) as c:
            ver = c.execute("SHOW server_version").fetchone()[0]  # type: ignore[index]
            vec = c.execute("SELECT 1 FROM pg_available_extensions WHERE name='vector'").fetchone()
            rd = c.execute("SELECT 1 FROM pg_roles WHERE rolname='benchbook_reader'").fetchone()
        st = status(url)
        cur = st.revision
    except psycopg.Error as e:
        # An unreachable team database must not hide the remaining checks (and --json must stay JSON).
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        checks[-1] = Check("connect", False, f"{mode}: {first}",
                           "check BENCHBOOK_DATABASE_URL and that the server is up")
        return [*checks, Check("db schema", False, "unknown (no connection)"), build_head]
    checks.append(Check("postgres >= 15", int(ver.split(".")[0]) >= 15, ver))
    checks.append(Check("pgvector (optional)", True, "available" if vec else "not available"))
    checks.append(Check("benchbook_reader role", rd is not None, "present" if rd else "missing", "benchbook migrate"))
    if st.legacy:  # migrate renames it in place; with both schemas present it says which one to drop
        checks.append(Check("db schema", False, f"pre-rename schema {LEGACY_SCHEMA} at {st.legacy_revision}"
                            + (f", next to benchbook at {cur}" if cur else ""), "benchbook migrate"))
    elif cur == head():
        checks.append(Check("db schema", True, cur))
    elif cur is None or cur < head():
        checks.append(Check("db schema", False, f"{cur or 'empty'} (older than this build)", "benchbook migrate"))
    else:
        checks.append(Check("db schema", False, f"{cur} (newer than this build)",
                            "upgrade benchbook: uv tool upgrade benchbook"))
    checks.append(build_head)
    from . import config as lc

    cfg = lc.load()
    checks.append(Check("project config", cfg.project is not None,
                        f"{cfg.project} ({cfg.path or 'BENCHBOOK_PROJECT'})" if cfg.project
                        else "no .benchbook/config.toml",
                        "benchbook init"))
    return checks


def _report(checks: list[Any]) -> None:
    ok = all(c.ok for c in checks)
    emit({"ok": ok, "checks": [{"check": c.name, "ok": c.ok, "detail": c.detail, "fix": None if c.ok else c.fix}
                               for c in checks]},
         "\n".join(f"{'ok  ' if c.ok else 'FAIL'}  {c.name:<22} {c.detail}"
                   + (f"\n      fix: {c.fix}" if not c.ok and c.fix else "") for c in checks))
    if not ok:
        raise typer.Exit(1)


@db_app.command("stop")
def db_stop() -> None:
    """Stop the local embedded Postgres (it keeps running between sessions otherwise)."""
    from .db.conn import stop_embedded

    stopped = stop_embedded()
    emit({"ok": stopped}, "stopped" if stopped else "no embedded database")


@db_app.command("url")
def db_url() -> None:
    """Print the database URL in use."""
    from .db.conn import database_url

    u = database_url()
    emit({"url": u}, u)


# ------------------------------------------------------------------ blobs and autoresearch

blob_app = typer.Typer(no_args_is_help=True, help="Small files stored in the ledger (content addressed).")
app.add_typer(blob_app, name="blob")


@blob_app.command("put")
def blob_put(path: Path, media_type: Annotated[str, typer.Option("--media-type")] = "application/octet-stream") -> None:
    """Store a file; prints benchbook://blob/<sha256>."""
    if not path.is_file():
        die(f"file not found: {path}")
    uri = ledger().put_blob(path.read_bytes(), media_type)
    emit({"ok": True, "uri": uri}, uri)


@blob_app.command("get")
def blob_get(uri: str, output: Annotated[Path | None, typer.Option("-o", "--output")] = None) -> None:
    """Write a stored blob to a file (or stdout)."""
    data = ledger(read_only=True).get_blob(uri)
    if output is not None:
        output.write_bytes(data)
        emit({"ok": True, "path": str(output), "bytes": len(data)}, str(output))
    else:
        sys.stdout.buffer.write(data)


def _register_ar() -> None:
    from .integrations.autoresearch.cli import ar_app

    app.add_typer(ar_app, name="ar")


_register_ar()


# ------------------------------------------------------------------ entry point

def _extract_globals(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            out += argv[i:]
            break
        if a == "--json":
            STATE["json"] = True
        elif a in ("--project", "--agent") and i + 1 < len(argv):
            STATE[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith(("--project=", "--agent=")):
            k, v = a[2:].split("=", 1)
            STATE[k] = v
        else:
            out.append(a)
        i += 1
    return out


def main(argv: list[str] | None = None) -> None:
    args = _extract_globals(list(sys.argv[1:] if argv is None else argv))
    # `init --project X` uses the value as the new project's id
    if args[:1] == ["init"] and STATE["project"]:
        args += ["--project", STATE["project"]]
    try:
        rc = app(args=args, prog_name="benchbook", standalone_mode=False)
        sys.exit(rc if isinstance(rc, int) else 0)
    except typer.Exit as e:
        sys.exit(e.exit_code)
    except Conflict as e:
        try:
            die(str(e), 2)
        except typer.Exit as x:
            sys.exit(x.exit_code)
    except ValidationFailed as e:
        if STATE["json"]:
            print(json.dumps({"ok": False, "error": "validation failed", "problems": e.problems}, ensure_ascii=False))
        else:
            print(str(e), file=sys.stderr)
        sys.exit(1)
    except BenchbookError as e:
        try:
            die(str(e))
        except typer.Exit as x:
            sys.exit(x.exit_code)
    except Exception as e:
        # click usage errors (typer vendors click, so match by shape rather than class)
        name = type(e).__name__
        if name == "NoArgsIsHelpError":
            sys.exit(0)
        if hasattr(e, "show") and hasattr(e, "exit_code"):
            e.show()
            sys.exit(1)  # usage errors are user errors; 2 is reserved for duplicates
        if name == "Abort":
            sys.exit(1)
        raise


if __name__ == "__main__":
    # Run through the canonical module: under `python -m benchbook.cli` this file is __main__, and the
    # sub-apps (benchbook ar) would otherwise read a second, empty STATE from benchbook.cli.
    from benchbook.cli import main as _main

    _main()
