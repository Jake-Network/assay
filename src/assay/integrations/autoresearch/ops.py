"""assay ar: the operations behind each command (§8). The CLI in cli.py only formats their results.

Guarantees come from this wrapper rather than from agent discipline (R2): a run is refused before it
starts when the same code already ran or is running in the partition, every run that starts is recorded
(a local queue keeps it when the database is unreachable), and every run ends with a referee tick.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from ...dedup import canonical_json
from ...errors import AssayError, Conflict, LeaseLost, NotFound
from ...ids import short
from ...ledger import Ledger
from . import hw as hwmod
from . import referee
from .checkout import Checkout
from .code import DEFAULT_KNOB_IGNORE, changed_knobs, code_hash, extract_knobs, knob_text, short_hash
from .runner import (
    Interrupted,
    RunResult,
    append_results_tsv,
    check_budget,
    parse_summary,
    results_tsv_line,
    run_command,
    tail,
)

DEFAULT_CMD = "uv run train.py"
DEFAULT_TIMEOUT = 600
METRIC = "val_bpb"
PROJECT_SETTINGS: dict[str, Any] = {
    "partition_key": "partition", "worker_intents": True, "outcome_baseline": "verified",
    "knob_ignore": list(DEFAULT_KNOB_IGNORE), "replicates": 2, "replicate_max_queue": 4, "sanity_min": 0.5,
    "sanity_max_gain": 0.1, "epsilon_floor": 0.0005, "rerun_min_seeds": 3, "rerun_min_authors": 2,
    "blob_max_bytes": 1_000_000, "time_budget_slack_s": 30, "time_budget_overhead_s": 240,
}
GOAL = "Lowest val_bpb in the fixed training-time budget (autoresearch); only replicated results count."
INTENT_LEASE = 900


class Refused(AssayError):
    """The command did not run because of a duplicate or conflict (exit code 2)."""

    def __init__(self, message: str, data: dict[str, Any] | None = None):
        self.data = data or {}
        super().__init__(message)


@dataclass
class Ctx:
    lg: Ledger
    co: Checkout
    hw: str
    partition: str

    @property
    def settings(self) -> dict[str, Any]:
        return self.lg.project.settings


def host_slug(hostname: str | None = None) -> str:
    """The whole host name, lower-case, runs of other characters as '-' (A6: a prefix collides across
    gpu-node-12 and gpu-node-13). Over 40 characters it is cut and a short hash keeps it unique."""
    raw = socket.gethostname() if hostname is None else hostname
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "host"
    if len(slug) > 40:
        slug = slug[:35].rstrip("-") + "-" + hashlib.sha256(raw.encode()).hexdigest()[:4]
    return slug


def default_agent_name(env: dict[str, str] | None = None, hostname: str | None = None) -> str:
    env = dict(os.environ if env is None else env)
    dev = (env.get("CUDA_VISIBLE_DEVICES") or "0").split(",")[0].strip() or "0"
    return f"gpu{dev}-{host_slug(hostname)}"


def open_ctx(project: str | None = None, agent: str | None = None, hw: str | None = None,
             start: Path | None = None) -> Ctx:
    co = Checkout.find(start)
    agent = agent or os.environ.get("ASSAY_AGENT")
    if not agent:
        raise AssayError(f"set ASSAY_AGENT (one name per GPU), e.g. export ASSAY_AGENT={default_agent_name()}")
    lg = Ledger.open(project=project, agent=agent)
    if lg.project.pack != "ml-research":
        raise AssayError(f"project {lg.project_id} uses pack {lg.project.pack!r}; assay ar needs an ml-research "
                         "project (run assay ar init)")
    hw_class = hwmod.detect(hw)
    ctx = Ctx(lg, co, hw_class, co.partition(hw_class))
    flush_pending(ctx)
    return ctx


def better(lg: Ledger, a: float, b: float, eps: float = 0.0) -> bool:
    return a < b - eps if lg.project.metric_direction == "min" else a > b + eps


# ---------------------------------------------------------------------------------------------- init

def init(project: str, hw: str | None = None, start: Path | None = None, force: bool = False,
         database_url: str | None = None) -> dict[str, Any]:
    from ...config import write_config

    co = Checkout.find(start)
    p, created = Ledger.init_project(project, title=f"autoresearch {project}", goal=GOAL, metric=METRIC,
                                     direction="min", epsilon=PROJECT_SETTINGS["epsilon_floor"],
                                     settings=PROJECT_SETTINGS, pack="ml-research", database_url=database_url)
    if p.pack != "ml-research":
        raise AssayError(f"project {p.id} already exists with pack {p.pack!r}; choose another --project")
    lg = Ledger.open(project=p.id, agent="human", database_url=database_url)
    referee.referee_ledger(lg)
    cfg = write_config(co.root, p.id, budget=1500)
    program = co.root / "program.assay.md"
    template = resources.files("assay.integrations.autoresearch").joinpath("templates/program.assay.md")
    text = template.read_text(encoding="utf-8")
    wrote_program = False
    if force or not program.exists():
        program.write_text(text, encoding="utf-8")
        wrote_program = True
    gi = co.root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    add = [x for x in (".assay/ar_state.json", ".assay/ar_pending.jsonl", "run.log", "ar_run.out") if x not in lines]
    if add:
        with open(gi, "a", encoding="utf-8") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write("\n".join(add) + "\n")
    hw_class = hwmod.detect(hw)
    return {"project": p.id, "created": created, "partition": co.partition(hw_class), "hw": hw_class,
            "config": str(cfg), "program": str(program), "wrote_program": wrote_program,
            "program_differs": not wrote_program and program.read_text(encoding="utf-8") != text,
            "agent_suggestion": os.environ.get("ASSAY_AGENT") or default_agent_name(),
            "prompt": "Read program.assay.md and start. Use the assay ar commands for every run."}


# ---------------------------------------------------------------------------------------------- pending

PENDING_STALE_S = 600


def _append_line(p: Path, line: str) -> None:
    """One os.write on an O_APPEND descriptor: concurrent writers never interleave or lose lines (A5)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _pending_append(ctx: Ctx, record: dict[str, Any]) -> None:
    _append_line(ctx.co.pending_path(), json.dumps(record, default=str))


def _take_pending(p: Path) -> list[Path]:
    """Atomically move the queue (and batches left by a flusher that died) to files only this process reads.
    New runs keep appending to a fresh queue file meanwhile."""
    taken: list[Path] = []
    candidates = [p] + [f for f in p.parent.glob(p.name + ".*.flushing")
                        if time.time() - f.stat().st_mtime > PENDING_STALE_S]
    for src in candidates:
        dst = p.with_name(f"{p.name}.{os.getpid()}-{uuid4().hex[:8]}.flushing")
        try:
            os.replace(src, dst)
        except FileNotFoundError:
            continue  # another process took it
        os.utime(dst)
        taken.append(dst)
    return taken


def flush_pending(ctx: Ctx) -> int:
    """Record runs that finished while the ledger was unreachable, then complete their task or intent.
    Returns how many were written. Lines that still fail go back to the queue."""
    p = ctx.co.pending_path()
    if not p.parent.exists():
        return 0
    done = 0
    for batch in _take_pending(p):
        for line in batch.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                lg = ctx.lg if item.get("agent") in (None, ctx.lg.agent) else ctx.lg.as_agent(item["agent"])
                r = lg.record_experiment(**item["record"])
                if r.id is not None and item.get("parent_id"):
                    lg.link(r.id, "derived_from", item["parent_id"])
            except Exception:  # noqa: BLE001 - keep it for the next command
                _append_line(p, line)
                continue
            done += 1
            if c := item.get("complete"):
                try:
                    lg.complete_task(c["task"], "done", str(c.get("summary") or "")[:1000])
                except Exception:  # noqa: BLE001 - lease lost or already ended
                    pass
        batch.unlink()
    return done


# ---------------------------------------------------------------------------------------------- reads

def experiments_for_code(ctx: Ctx, code_ref: str, partition: str | None = None) -> list[dict[str, Any]]:
    return ctx.lg.sql_internal(
        "SELECT * FROM assay.experiments_valid WHERE project_id=%s AND code_ref=%s AND config->>'partition'=%s"
        " ORDER BY coalesce(seed,0), created_at, id", (ctx.lg.project_id, code_ref, partition or ctx.partition))


def partition_best(ctx: Ctx, statuses: tuple[str, ...] = ("verified",)) -> dict[str, Any] | None:
    rows = ctx.lg.sql_internal(
        "SELECT * FROM assay.v_partition_best WHERE project_id=%s AND partition=%s AND rank=1 AND status = ANY(%s)",
        (ctx.lg.project_id, ctx.partition, list(statuses)))
    rows = [r for r in rows if r["mean"] is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: r["mean"] if ctx.lg.project.metric_direction == "min" else -r["mean"])
    return rows[0]


def inflight(ctx: Ctx, others_only: bool = True) -> list[dict[str, Any]]:
    return ctx.lg.sql_internal(
        "SELECT * FROM assay.v_inflight WHERE project_id=%s AND is_intent AND partition=%s"
        " AND (NOT %s OR agent <> %s) ORDER BY created_at",
        (ctx.lg.project_id, ctx.partition, others_only, ctx.lg.agent))


def blob_of(exp: dict[str, Any]) -> str | None:
    for a in exp.get("artifacts") or []:
        if isinstance(a, dict) and str(a.get("uri", "")).startswith("assay://blob/"):
            return str(a["uri"])
    return None


# ---------------------------------------------------------------------------------------------- claim

_WORD = re.compile(r"[\w.]+", re.UNICODE)


def words(s: str) -> set[str]:
    return {w.strip(".") for w in _WORD.findall(s.lower()) if w.strip(".")}


def jaccard(a: str, b: str) -> float:
    x, y = words(a), words(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def claim(ctx: Ctx, description: str, knobs: dict[str, Any], force: bool = False) -> dict[str, Any]:
    description = description.strip()
    if not description:
        raise AssayError("describe the idea in one line")
    knob_key = f"{ctx.partition}|{canonical_json(knobs)}" if knobs else None
    warnings: list[str] = []
    similar = [r for r in inflight(ctx) if jaccard(description, r["title"]) >= 0.8]
    if similar and not force:
        s = similar[0]
        raise Refused(f"{short('task', s['id'])} by {s['agent']} is already trying something very similar: "
                      f"\"{s['title']}\". Pick another idea (or --force if it really differs).",
                      {"similar": short("task", s["id"]), "agent": s["agent"]})
    warnings += [f"similar idea in progress: {short('task', s['id'])} by {s['agent']}: \"{s['title']}\""
                 for s in similar]
    if knobs:
        done = ctx.lg.sql_internal(
            "SELECT id, author, outcome, primary_value, config->'changed' AS changed FROM assay.experiments_valid"
            " WHERE project_id=%s AND config->>'partition'=%s AND coalesce(seed,0)=0"
            " ORDER BY created_at DESC LIMIT 500", (ctx.lg.project_id, ctx.partition))
        want = {k: canonical_json(v) for k, v in knobs.items()}
        for d in done:
            ch = d["changed"] or {}
            if all(k in ch and canonical_json(ch[k][1]) == v for k, v in want.items()):
                val = "" if d["primary_value"] is None else f" {d['primary_value']:.6f}"
                warnings.append(f"already tried: {short('experiment', d['id'])} by {d['author']} -> "
                                f"{d['outcome']}{val} (its parent may differ from yours)")
                if len(warnings) >= 6:
                    break
    previous = ctx.lg.my_intents()
    spec = {"partition": ctx.partition, "knobs": knobs, "knob_key": knob_key, "description": description}
    try:
        t = ctx.lg.create_intent(description, spec, lease_seconds=INTENT_LEASE, exclusive=["knob_key"])
    except Conflict as e:
        raise Refused(f"{e} Pick another idea.", {"conflict": str(e), "agent": e.agent}) from e
    for old in previous:
        try:
            ctx.lg.complete_task(old.id, "failed", "abandoned: replaced by a new intent before running")
        except LeaseLost:
            pass
    return {"intent": short("task", t.id), "partition": ctx.partition, "knobs": knobs, "warnings": warnings,
            "lease_seconds": INTENT_LEASE}


# ---------------------------------------------------------------------------------------------- run

def run(ctx: Ctx, description: str, cmd: str = DEFAULT_CMD, timeout: float = DEFAULT_TIMEOUT,
        parse_regex: str | None = None, retry_failed: bool = False) -> dict[str, Any]:
    from .runner import DEFAULT_PARSE_REGEX

    description = description.strip() or "(no description)"
    lg, co = ctx.lg, ctx.co
    src = co.train_bytes()  # these bytes are hashed, stored and run (A8)
    code_ref = code_hash(src)
    knobs = extract_knobs(src)
    settings = lg.refresh().settings
    ignore = settings.get("knob_ignore", DEFAULT_KNOB_IGNORE)
    parent_hash, parent_knobs, how = co.parent(src)
    if parent_hash == code_ref:  # nothing changed relative to the parent (e.g. a baseline re-run)
        parent_hash, parent_knobs = None, None
    config = {"partition": ctx.partition, "knobs": knobs,
              "changed": changed_knobs(parent_knobs, knobs, ignore) if parent_knobs is not None else {},
              "parent": parent_hash, "description": description[:500]}
    # ② finished duplicate
    tried = lg.check_tried(config, code_ref)
    ok_runs = [e for e in tried.exact if e.run_status == "ok"]
    if ok_runs or (tried.exact and not retry_failed):
        prev = ok_runs[0] if ok_runs else tried.exact[0]
        val = "" if prev.primary_value is None else f" {METRIC}={prev.primary_value:.6f}"
        raise Refused(f"DUPLICATE: this code already ran in {ctx.partition}: {short('experiment', prev.id)} by "
                      f"{prev.author} -> {prev.outcome}{val}. Not run. Change train.py"
                      + ("" if ok_runs else " (or pass --retry-failed to retry a failed run)") + ".",
                      {"duplicate_of": short("experiment", prev.id), "outcome": prev.outcome,
                       "value": prev.primary_value})
    # ③ intent (and ② running duplicate)
    lease = int(timeout) + 300
    patch = {"partition": ctx.partition, "code_hash": code_ref, "code_key": f"{ctx.partition}|{code_ref}",
             "description": description, "running": True}
    mine = lg.my_intents()
    try:
        if mine:
            intent = lg.update_intent(mine[0].id, patch, lease_seconds=lease, exclusive=["code_key"])
        else:
            intent = lg.create_intent(description, {**patch, "auto": True}, lease_seconds=lease,
                                      exclusive=["code_key"])
    except Conflict as e:
        raise Refused(f"DUPLICATE: the same code is running right now: {e}. Not run.", {"running": str(e)}) from e
    again = lg.sql_internal("SELECT id FROM assay.experiments_valid WHERE project_id=%s AND dedup_key=%s"
                            " AND run_status='ok' LIMIT 1", (lg.project_id, tried.dedup_key))
    if again:
        lg.complete_task(intent.id, "failed", "duplicate finished while starting")
        raise Refused(f"DUPLICATE: this code finished meanwhile: {short('experiment', again[0]['id'])}. Not run.",
                      {"duplicate_of": short("experiment", again[0]["id"])})
    baseline, eps = lg.baseline(ctx.partition)
    uri = lg.put_blob(src, "text/x-python")
    # ④⑤ run the hashed bytes (not the working file, which the agent may edit meanwhile) and parse
    res, interrupted = _run_isolated(ctx, src, cmd, timeout, parse_regex or DEFAULT_PARSE_REGEX, co.root / "run.log",
                                     {"ASSAY_AR_RUN": "run", "ASSAY_AR_SEED": "0", "ASSAY_AR_CODE": code_ref})
    notes = description
    if res.run_status != "ok":
        notes = f"{description}\n[{res.reason}] run.log tail:\n{res.log_tail}"[:2000]
    value = res.metrics.get(METRIC) if res.run_status == "ok" else None
    # A1: KEEP means better than this agent's parent; the server's outcome (against the verified best)
    # decides only whether the run is a candidate for replication.
    parent_value = code_value(ctx, parent_hash) if parent_hash else None
    beats_parent = None if value is None or parent_value is None else better(lg, float(value), parent_value, eps)
    record: dict[str, Any] = {"config": config, "metrics": res.metrics if res.run_status == "ok" else
                              _numeric(res.metrics),
              "run_status": res.run_status, "seed": 0, "code_ref": code_ref, "notes": notes,
              "artifacts": [{"uri": uri, "kind": "train.py", "sha256": hashlib.sha256(src).hexdigest(),
                             "bytes": len(src)}],
              "task_id": str(intent.id), "duration_ms": res.duration_ms,
              "trace_ref": {"cmd": cmd, "parse_regex": parse_regex, "timeout": timeout,
                            "parent_value": parent_value, "beats_parent": beats_parent}}
    parent_exp = None
    if parent_hash:
        rows = experiments_for_code(ctx, parent_hash)
        parent_exp = rows[0]["id"] if rows else None
    # ⑥ record (a failure to record is queued locally, never dropped)
    rec = None
    outcome0 = "failed" if res.run_status != "ok" else "queued"
    summary = f"{outcome0} {METRIC}={value}"
    try:
        rec = lg.record_experiment(**record)
        if rec.id is not None and parent_exp is not None:
            lg.link(rec.id, "derived_from", parent_exp)
    except Exception as e:  # noqa: BLE001
        # ⑦ the intent stays open (it keeps the code reserved) until the queued run is written (A5)
        _pending_append(ctx, {"agent": lg.agent, "record": record, "parent_id": str(parent_exp or "") or None,
                              "complete": {"task": str(intent.id), "summary": summary}, "error": str(e)})
        print(f"warning: could not record the run ({e}); queued in {co.pending_path()} and retried by the "
              "next assay ar command", file=sys.stderr)
    outcome = rec.outcome if rec is not None and rec.outcome else outcome0
    keep = outcome == "improved" and beats_parent is not False
    # ⑦ intent done, only once the run is in the ledger
    if rec is not None:
        try:
            lg.complete_task(intent.id, "done", f"{outcome} {METRIC}={value}"[:1000])
        except Exception:  # noqa: BLE001 - LeaseLost or the database went away; the lease runs out
            pass
    # ⑧ results.tsv (upstream format)
    head = co.git_show("HEAD") if co.is_git() else None
    commit = co.short_commit() if head is not None and code_hash(head) == code_ref else None
    tsv_status = "keep" if keep else ("crash" if res.run_status != "ok" else "discard")
    append_results_tsv(co.root / "results.tsv", results_tsv_line(
        commit or short_hash(code_ref, 7), value if tsv_status != "crash" else None,
        res.metrics.get("peak_vram_mb") if tsv_status != "crash" else None, tsv_status, description))
    # local state
    if rec is not None and rec.id is not None:
        if keep:
            co.save_state(last_experiment=str(rec.id), last_code_hash=code_ref, last_knobs=knobs)
        else:
            co.save_state(last_experiment=str(rec.id), last_code_hash=parent_hash, last_knobs=parent_knobs)
    if interrupted is not None:
        raise interrupted
    # ⑨ referee
    actions = safe_tick(lg)
    if rec is not None and rec.duplicate_of is not None:
        verdict = f"DUPLICATE (recorded meanwhile as {short('experiment', rec.duplicate_of)})"
    elif keep:
        verdict = "KEEP (baseline, calibration pending)" if baseline is None else \
            "KEEP (candidate, replication pending)"
    elif outcome == "improved" and parent_value is not None:
        verdict = (f"DISCARD (not better than its parent {parent_value:.6f} by epsilon {eps:.6f}; it still beats "
                   "the verified best, so the referee may replicate it)")
    elif outcome == "no_change":
        verdict = f"DISCARD (no change: within epsilon {eps:.6f} of {baseline:.6f})" if baseline is not None \
            else "DISCARD (no change)"
    elif outcome == "worse":
        verdict = f"DISCARD (worse by {abs(value - baseline):.6f})" if baseline is not None and value is not None \
            else "DISCARD (worse)"
    elif outcome == "queued":
        verdict = ("KEEP? (not recorded yet; decide after the next assay ar command)" if res.run_status == "ok"
                   else f"CRASH ({res.reason})")
    elif res.run_status == "invalid":
        verdict = f"INVALID ({res.reason})"
    else:
        verdict = f"CRASH ({res.reason})"
    return {"verdict": verdict, "outcome": outcome, "keep": keep, "value": value, "baseline": baseline,
            "parent_value": parent_value, "epsilon": eps,
            "id": short("experiment", rec.id) if rec is not None and rec.id else None,
            "code": short_hash(code_ref), "parent": short_hash(parent_hash) if parent_hash else None,
            "parent_from": how, "changed": config["changed"], "run_status": res.run_status,
            "duration_s": round(res.duration_ms / 1000, 1), "referee": actions}


def code_value(ctx: Ctx, code_ref: str) -> float | None:
    """Mean val_bpb over the successful runs of this code in the partition (first run and replications)."""
    vals = [float(r["primary_value"]) for r in experiments_for_code(ctx, code_ref)
            if r["run_status"] == "ok" and r["primary_value"] is not None]
    return statistics.fmean(vals) if vals else None


def time_budget(ctx: Ctx) -> tuple[float | None, float, float | None]:
    """(training budget s, slack s, wall-clock allowance s) for the validity check (A9). The budget is the
    time_budget_s setting, else TIME_BUDGET in prepare.py, else 300; time_budget_s 0 turns the check off."""
    s = ctx.lg.project.settings
    budget = s.get("time_budget_s")
    if budget is None:
        tb = extract_knobs((ctx.co.root / "prepare.py").read_bytes()).get("TIME_BUDGET")
        budget = tb if isinstance(tb, (int, float)) and not isinstance(tb, bool) else 300
    overhead = s.get("time_budget_overhead_s", 240)
    return (float(budget) or None, float(s.get("time_budget_slack_s", 30)),
            None if overhead is None else float(overhead))


def _run_isolated(ctx: Ctx, src: bytes, cmd: str, timeout: float, parse_regex: str, log_path: Path,
                  env: dict[str, str]) -> tuple[RunResult, BaseException | None]:
    """Run `src` as train.py in a temporary copy of the checkout. An interruption (Ctrl-C, SIGTERM, SIGHUP)
    kills the training, becomes an error result and is returned so the caller records the run first (A4)."""
    tmp = _isolated_dir(ctx.co, src)
    t0 = time.monotonic()
    interrupted: BaseException | None = None
    try:
        res = run_command(cmd, tmp, log_path, timeout, METRIC, parse_regex, env={**os.environ, **env})
    except (KeyboardInterrupt, Interrupted) as e:
        interrupted = e
        text = log_path.read_bytes().decode("utf-8", errors="replace") if log_path.exists() else ""
        name = e.args[0] if isinstance(e, Interrupted) else "SIGINT"
        res = RunResult("error", None, parse_summary(text, parse_regex), int((time.monotonic() - t0) * 1000),
                        tail(text), f"interrupted ({name})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if interrupted is None:
        budget, slack, overhead = time_budget(ctx)
        res = check_budget(res, budget, slack, overhead)
    return res, interrupted


def _numeric(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def safe_tick(lg: Ledger) -> list[str]:
    try:
        return referee.tick(lg)
    except Exception as e:  # noqa: BLE001 - the run is recorded; the next tick catches up
        print(f"warning: referee tick failed ({e}); the next assay ar command retries", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------------------------- replicate

def _isolated_dir(co: Checkout, src: bytes) -> Path:
    """§8.1: a temp dir with symlinks to every top-level entry of the checkout except train.py (and
    git, logs, results), plus the code under replication as train.py."""
    tmp = Path(tempfile.mkdtemp(prefix="assay-replicate-"))
    skip = {"train.py", ".git", "run.log", "results.tsv", ".assay"}
    for entry in co.root.iterdir():
        if entry.name in skip:
            continue
        dst = tmp / entry.name
        try:
            dst.symlink_to(entry, target_is_directory=entry.is_dir())
        except OSError:
            if entry.is_file():
                shutil.copy2(entry, dst)
    (tmp / "train.py").write_bytes(src)
    return tmp


def replicate(ctx: Ctx, max_tasks: int = 1, cmd: str = DEFAULT_CMD, timeout: float = DEFAULT_TIMEOUT,
              parse_regex: str | None = None) -> list[dict[str, Any]]:
    """Run replication/calibration tasks. The task's own command (the one the origin run used) wins over
    `cmd` (A9), so forks that need --cmd replicate correctly."""
    from .runner import DEFAULT_PARSE_REGEX

    lg, co = ctx.lg, ctx.co
    done: list[dict[str, Any]] = []
    for _ in range(max_tasks):
        t = lg.next_task(tags=["replicate", "calibration"], lease_seconds=int(timeout) + 300,
                         spec_match={"partition": ctx.partition})
        if t is None:
            break
        spec = t.spec
        try:
            src = lg.get_blob(str(spec.get("code_blob")))
        except NotFound as e:
            lg.complete_task(t.id, "failed", f"code blob missing: {e}"[:1000])
            continue
        if code_hash(src) != spec.get("code_ref"):
            lg.complete_task(t.id, "failed", "code blob does not match code_ref")
            continue
        if co.partition(ctx.hw) != spec.get("partition"):
            lg.complete_task(t.id, "failed", "prepare.py differs from the task's harness; not run")
            continue
        origin = lg.get(str(spec["origin"]))["record"]
        run_cmd = str(spec.get("cmd") or cmd)
        regex = spec.get("parse_regex") or parse_regex or DEFAULT_PARSE_REGEX
        tmp_log = Path(tempfile.mkdtemp(prefix="assay-replicate-log-")) / "run.log"
        try:
            res, interrupted = _run_isolated(ctx, src, run_cmd, timeout, regex, tmp_log,
                                             {"ASSAY_AR_RUN": str(spec.get("kind") or "replicate"),
                                              "ASSAY_AR_SEED": str(spec.get("seed")),
                                              "ASSAY_AR_CODE": str(spec["code_ref"])})
        finally:
            shutil.rmtree(tmp_log.parent, ignore_errors=True)
        notes = f"replication {spec.get('seed')} of {short('experiment', origin['id'])}"
        if res.run_status != "ok":
            notes = f"{notes}\n[{res.reason}] run.log tail:\n{res.log_tail}"[:2000]
        record: dict[str, Any] = {"config": origin["config"], "metrics": res.metrics if res.run_status == "ok" else
                  _numeric(res.metrics), "run_status": res.run_status, "seed": int(spec["seed"]),
                  "code_ref": spec["code_ref"], "notes": notes, "artifacts": origin.get("artifacts") or [],
                  "task_id": str(t.id), "duration_ms": res.duration_ms,
                  "trace_ref": {"cmd": run_cmd, "parse_regex": parse_regex, "timeout": timeout}}
        value = res.metrics.get(METRIC) if res.run_status == "ok" else None
        summary = f"{res.run_status} {METRIC}={value}"
        rec = None
        try:
            rec = lg.record_experiment(**record)
        except Exception as e:  # noqa: BLE001
            # The task stays claimed until the queued run is written (A5): completing it now would let the
            # referee judge the candidate without this run.
            _pending_append(ctx, {"agent": lg.agent, "record": record, "error": str(e),
                                  "complete": {"task": str(t.id), "summary": summary}})
            print(f"warning: could not record the replication ({e}); queued in {co.pending_path()}",
                  file=sys.stderr)
        if rec is not None:
            try:
                lg.complete_task(t.id, "done", summary)
            except LeaseLost:
                pass
        if interrupted is not None:
            raise interrupted
        actions = safe_tick(lg) if rec is not None else []
        done.append({"task": short("task", t.id), "kind": spec.get("kind"), "origin": short("experiment", origin["id"]),
                     "seed": spec.get("seed"), "run_status": res.run_status, "reason": res.reason, "value": value,
                     "id": short("experiment", rec.id) if rec is not None and rec.id else None,
                     "queued": rec is None, "duration_s": round(res.duration_ms / 1000, 1), "referee": actions})
    return done


# ---------------------------------------------------------------------------------------------- sync

def _bad_ancestor(ctx: Ctx, code_ref: str) -> str | None:
    """A refuted or inconclusive result on this code or any of its ancestors."""
    rows = experiments_for_code(ctx, code_ref)
    seen: set[UUID] = set()
    frontier = [r["id"] for r in rows if (r["seed"] or 0) == 0]
    while frontier:
        eid = frontier.pop()
        if eid in seen:
            continue
        seen.add(eid)
        bad = ctx.lg.sql_internal(
            "SELECT s.status FROM assay.links l JOIN assay.v_claim_status s ON s.id = l.dst_id"
            " WHERE l.project_id=%s AND l.src_id=%s AND l.relation='supports' AND s.kind='result'"
            " AND s.status IN ('refuted','inconclusive')", (ctx.lg.project_id, eid))
        if bad:
            return f"{short('experiment', eid)} was {bad[0]['status']}"
        frontier += [r["parent_id"] for r in ctx.lg.sql_internal(
            "SELECT parent_id FROM assay.v_lineage WHERE project_id=%s AND id=%s AND parent_id IS NOT NULL",
            (ctx.lg.project_id, eid))]
    return None


def sync(ctx: Ctx, trust: str = "verified") -> dict[str, Any]:
    if trust not in ("verified", "candidate"):
        raise AssayError("--trust must be verified or candidate")
    lg, co = ctx.lg, ctx.co
    target = partition_best(ctx, ("verified", "candidate") if trust == "candidate" else ("verified",))
    if target is None:
        return {"adopted": False, "reason": f"no {trust} result in {ctx.partition} yet", "partition": ctx.partition}
    src = co.train_bytes()
    mine = code_hash(src)
    if mine == target["code_ref"]:
        return {"adopted": False, "reason": "you already have it", "code": short_hash(mine),
                "value": target["mean"], "partition": ctx.partition}
    my_value = code_value(ctx, mine)
    bad = _bad_ancestor(ctx, mine)
    if my_value is not None and not better(lg, float(target["mean"]), my_value) and bad is None:
        return {"adopted": False, "reason": f"your code ({my_value:.6f}) is at least as good", "partition":
                ctx.partition, "mine": my_value, "target": target["mean"]}
    origin = lg.get(str(target["origin_id"]))["record"]
    uri = blob_of(origin)
    if uri is None:
        raise AssayError(f"the {target['status']} best has no stored code")
    code = lg.get_blob(uri)
    co.train_path.write_bytes(code)
    h8 = short_hash(target["code_ref"])
    committed = False
    if co.is_git():
        co.git("add", "train.py")
        msg = f"adopt {target['status']} best {h8} ({METRIC}={target['mean']:.6f})"
        r = co.git("commit", "-m", msg, "--", "train.py")
        committed = r is not None and r.returncode == 0
    co.save_state(last_code_hash=target["code_ref"], last_knobs=extract_knobs(code))
    safe_tick(lg)
    why = bad or ("your code has no successful run yet" if my_value is None else
                  f"{target['mean']:.6f} beats your {my_value:.6f}")
    return {"adopted": True, "code": h8, "value": target["mean"], "status": target["status"], "why": why,
            "committed": committed, "partition": ctx.partition}


# ---------------------------------------------------------------------------------------------- notes

def note(ctx: Ctx, text: str, evidence: str | None = "last") -> dict[str, Any]:
    ev: list[tuple[str, str]] = []
    if evidence == "last":
        last = ctx.co.load_state().get("last_experiment")
        if last:
            ev = [(last, "supports")]
    elif evidence:
        ev = [(evidence, "supports")]
    r = ctx.lg.propose_claim("observation", text, payload={"partition": ctx.partition}, tags=[ctx.partition],
                             evidence=ev)
    return {"id": short("claim", r.id), "duplicate_of": short("claim", r.duplicate_of) if r.duplicate_of else None}


def idea(ctx: Ctx, text: str, knobs: dict[str, Any]) -> dict[str, Any]:
    r = ctx.lg.propose_claim("hypothesis", text, payload={"partition": ctx.partition, "knobs": knobs},
                             tags=[ctx.partition])
    return {"id": short("claim", r.id), "duplicate_of": short("claim", r.duplicate_of) if r.duplicate_of else None}


# ---------------------------------------------------------------------------------------------- import (S3)

def import_results(ctx: Ctx, path: Path, hw: str | None = None) -> dict[str, Any]:
    """Record an existing results.tsv: each row's train.py comes from `git show <commit>:train.py`.
    Imported runs are first runs (replication number 0); improvements become candidates and get replicated.
    `hw` is the hardware class the old runs used and must be given (A9): nothing in results.tsv says which GPU
    produced it. The partition's harness hash comes from prepare.py at each row's commit."""
    import csv

    lg, co = ctx.lg, ctx.co
    if not path.is_file():
        raise AssayError(f"no such file: {path}")
    if not hw:
        raise AssayError(f"pass --hw with the hardware class the imported runs used (this machine: {ctx.hw}); "
                         "results from other GPUs must not enter this machine's partition")
    hw_class = hwmod.slug(hw)
    if not co.is_git():
        raise AssayError("assay ar import needs the git history of this checkout")
    counts = {"imported": 0, "duplicates": 0, "missing_commit": 0, "rows": 0}
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    for row in rows:
        counts["rows"] += 1
        commit = (row.get("commit") or "").strip()
        src = co.git_show(commit) if commit else None
        if src is None:
            counts["missing_commit"] += 1
            continue
        code_ref = code_hash(src)
        knobs = extract_knobs(src)
        harness = co.git_show(commit, "prepare.py")
        part = f"{hw_class}/{short_hash(code_hash(harness))}" if harness is not None else co.partition(hw_class)
        prev = co.git_show(f"{commit}~1")
        parent_hash = code_hash(prev) if prev is not None else None
        if parent_hash == code_ref:
            parent_hash, prev = None, None
        ignore = ctx.settings.get("knob_ignore", DEFAULT_KNOB_IGNORE)
        description = (row.get("description") or "").strip()[:500]
        config = {"partition": part, "knobs": knobs,
                  "changed": changed_knobs(extract_knobs(prev), knobs, ignore) if prev is not None else {},
                  "parent": parent_hash, "description": description}
        status = (row.get("status") or "").strip().lower()
        try:
            val = float(row.get("val_bpb") or 0)
            mem = float(row.get("memory_gb") or 0)
        except ValueError:
            val, mem = 0.0, 0.0
        ok = status != "crash" and val > 0
        if any(f"commit {commit}," in (e["notes"] or "") for e in experiments_for_code(ctx, code_ref, part)):
            counts["duplicates"] += 1  # imported before (failed rows are not caught by the dedup index)
            continue
        uri = lg.put_blob(src, "text/x-python")
        rec = lg.record_experiment(
            config, {METRIC: val, "peak_vram_mb": mem * 1024} if ok else {}, run_status="ok" if ok else "error",
            seed=0, code_ref=code_ref, notes=f"{description} (imported from results.tsv, commit {commit}, {status})",
            artifacts=[{"uri": uri, "kind": "train.py", "sha256": hashlib.sha256(src).hexdigest(),
                        "bytes": len(src)}],
            trace_ref={"imported_from": path.name, "commit": commit, "hw": hw_class})
        if rec.duplicate_of is not None or rec.id is None:
            counts["duplicates"] += 1
            continue
        counts["imported"] += 1
        if parent_hash:
            parents = experiments_for_code(ctx, parent_hash, part)
            if parents:
                lg.link(rec.id, "derived_from", parents[0]["id"])
    counts["referee"] = len(safe_tick(lg))
    return counts


# ---------------------------------------------------------------------------------------------- status

# B5: a duplicate counts as blocked only when it was refused before running: 'avoided' hits that the same agent
# did not follow with a run of that code anyway (--retry-failed). 'rejected' hits are runs that already
# happened (a race, or a queued run replayed) and are not blocked runs.
BLOCKED_SQL = (
    "SELECT count(*) AS n, coalesce(sum(e.duration_ms),0)/60000.0 AS min FROM assay.dedup_hits h"
    " JOIN assay.experiments e ON e.id = h.existing_id WHERE h.project_id=%s AND e.config->>'partition'=%s"
    " AND h.kind='avoided' AND NOT EXISTS (SELECT 1 FROM assay.experiments x WHERE x.project_id=h.project_id"
    "   AND x.dedup_key=h.dedup_key AND x.author=h.agent_id AND coalesce(x.seed,0)=0"
    "   AND x.created_at >= h.created_at)")


def status(lg: Ledger, partition: str | None = None) -> dict[str, Any]:
    pid = lg.project_id
    lg.refresh()
    parts = [r["p"] for r in lg.sql_internal(
        "SELECT DISTINCT config->>'partition' AS p FROM assay.experiments_valid WHERE project_id=%s"
        " AND config->>'partition' IS NOT NULL ORDER BY 1", (pid,))]
    if partition:
        parts = [p for p in parts if p == partition] or [partition]
    cal = lg.project.settings.get("partitions") or {}
    out = []
    for p in parts:
        best = lg.sql_internal("SELECT * FROM assay.v_partition_best WHERE project_id=%s AND partition=%s"
                               " AND rank=1 AND status='verified'", (pid, p))
        cands = lg.sql_internal("SELECT * FROM assay.v_partition_best WHERE project_id=%s AND partition=%s"
                                " AND status='candidate' ORDER BY rank", (pid, p))
        fly = lg.sql_internal("SELECT agent, title, lease_left_s, spec->'knobs' AS knobs, id FROM assay.v_inflight"
                              " WHERE project_id=%s AND partition=%s AND is_intent ORDER BY created_at", (pid, p))
        queue = lg.sql_internal(
            "SELECT status, count(*) AS n FROM assay.tasks WHERE project_id=%s AND spec->>'partition'=%s"
            " AND tags && ARRAY['replicate','calibration'] GROUP BY status", (pid, p))
        gpu = lg.sql_internal(
            "SELECT coalesce(sum(duration_ms),0)/60000.0 AS total_min,"
            " coalesce(sum(duration_ms) FILTER (WHERE coalesce(seed,0) > 0),0)/60000.0 AS replicate_min,"
            " count(*) AS runs, count(*) FILTER (WHERE run_status <> 'ok') AS failed"
            " FROM assay.experiments WHERE project_id=%s AND config->>'partition'=%s", (pid, p))[0]
        blocked = lg.sql_internal(BLOCKED_SQL, (pid, p))[0]
        verdicts = {r["status"]: r["n"] for r in lg.sql_internal(
            "SELECT status, count(*) AS n FROM assay.v_claim_status s JOIN assay.claims c ON c.id=s.id"
            " WHERE s.project_id=%s AND s.kind='result' AND c.payload->>'partition'=%s GROUP BY status", (pid, p))}
        b = best[0] if best else None
        out.append({
            "partition": p,
            "verified_best": None if b is None else {"mean": b["mean"], "std": b["std"], "n": b["n"],
                                                     "code": short_hash(b["code_ref"]), "author": b["author"],
                                                     "claim": short("claim", b["ref_id"])},
            "candidates": [{"value": c["mean"], "id": short("experiment", c["ref_id"]), "author": c["author"],
                            "code": short_hash(c["code_ref"])} for c in cands],
            "in_flight": [{"agent": f["agent"], "idea": f["title"], "knobs": f["knobs"], "lease_left_s":
                           f["lease_left_s"], "id": short("task", f["id"])} for f in fly],
            "replication_tasks": {r["status"]: r["n"] for r in queue},
            "results": verdicts,
            "calibration": cal.get(p),
            "gpu_minutes": {"total": round(float(gpu["total_min"]), 1),
                            "replication": round(float(gpu["replicate_min"]), 1),
                            "replication_share": (round(float(gpu["replicate_min"]) / float(gpu["total_min"]), 3)
                                                  if float(gpu["total_min"]) else 0.0)},
            "runs": gpu["runs"], "failed_runs": gpu["failed"],
            "duplicates_blocked": blocked["n"], "gpu_minutes_saved": round(float(blocked["min"]), 1),
        })
    return {"project": pid, "partitions": out}


def format_knobs(knobs: dict[str, Any]) -> str:
    return ", ".join(f"{k}={knob_text(v)}" for k, v in knobs.items())
