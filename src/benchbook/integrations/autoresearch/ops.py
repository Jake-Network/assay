"""benchbook ar: the operations behind each command (§8). The CLI in cli.py only formats their results.

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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from ... import config as localcfg
from ...blobs import artifact_blob, blob_digest, blob_uris
from ...dedup import canonical_json
from ...errors import BenchbookError, Conflict, LeaseLost, NotFound
from ...ids import short
from ...ledger import Ledger
from ...models import RecordResult
from . import hw as hwmod
from . import referee, skills
from .checkout import Checkout, partition_keys
from .code import DEFAULT_KNOB_IGNORE, changed_knobs, code_hash, code_hashes, extract_knobs, knob_text, short_hash
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


class Refused(BenchbookError):
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
    partitions: tuple[str, ...]

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
    agent = agent or localcfg.env("AGENT")
    if not agent:
        raise BenchbookError(
            f"set BENCHBOOK_AGENT (one name per GPU), e.g. export BENCHBOOK_AGENT={default_agent_name()}")
    lg = Ledger.open(project=project, agent=agent)
    if lg.project.pack != "ml-research":
        raise BenchbookError(f"project {lg.project_id} uses pack {lg.project.pack!r}; "
                             "benchbook ar needs an ml-research project (run benchbook ar init)")
    hw_class = hwmod.detect(hw)
    parts = co.partition_keys(hw_class)
    ordered = _existing_partition_first(lg, parts)
    ctx = Ctx(lg, co, hw_class, ordered[0], tuple(dict.fromkeys((parts[0], *ordered))))
    if flush_pending(ctx):
        safe_tick(lg)
    return ctx


def _existing_partition_first(lg: Ledger, parts: tuple[str, ...]) -> tuple[str, ...]:
    """Preserve the original partition's history and baseline during an upgrade."""
    primary, *legacy = parts
    saved = (lg.project.settings.get("ar_partition_aliases") or {}).get(primary, {})
    if not legacy:
        return (*sorted(saved), primary) if saved else parts
    seen = lg.sql_internal(
        "SELECT config->>'partition' AS part FROM benchbook.experiments WHERE project_id=%(p)s"
        " AND config->>'partition'=ANY(%(parts)s) UNION"
        " SELECT spec->>'partition' AS part FROM benchbook.tasks WHERE project_id=%(p)s"
        " AND spec->>'partition'=ANY(%(parts)s)",
        {"p": lg.project_id, "parts": list(legacy)})
    existing = {r["part"] for r in seen} | set(lg.project.settings.get("partitions") or {})
    discovered = {part: True for part in legacy if part in existing and part not in saved}
    if discovered:
        # Another interpreter can reuse the alias once a worker has identified the old harness hash.
        referee.referee_ledger(lg).update_settings({"ar_partition_aliases": {primary: discovered}})
        lg.refresh()
        saved = lg.project.settings["ar_partition_aliases"][primary]
    return tuple(dict.fromkeys((*sorted(saved), *parts)))


def better(lg: Ledger, a: float, b: float, eps: float = 0.0) -> bool:
    return a < b - eps if lg.project.metric_direction == "min" else a > b + eps


# ---------------------------------------------------------------------------------------------- init

def init(project: str, hw: str | None = None, start: Path | None = None, force: bool = False,
         database_url: str | None = None, program: bool = True) -> dict[str, Any]:
    """`program` False leaves program.benchbook.md out: a checkout driven by a skill keeps the skill's own loop."""
    co = Checkout.find(start)
    p, created = Ledger.init_project(project, title=f"autoresearch {project}", goal=GOAL, metric=METRIC,
                                     direction="min", epsilon=PROJECT_SETTINGS["epsilon_floor"],
                                     settings=PROJECT_SETTINGS, pack="ml-research", database_url=database_url)
    if p.pack != "ml-research":
        raise BenchbookError(f"project {p.id} already exists with pack {p.pack!r}; choose another --project")
    lg = Ledger.open(project=p.id, agent="human", database_url=database_url)
    referee.referee_ledger(lg)
    cfg = localcfg.write_config(co.root, p.id, budget=1500)
    program_path = co.root / "program.benchbook.md"
    template = resources.files("benchbook.integrations.autoresearch").joinpath("templates/program.benchbook.md")
    text = template.read_text(encoding="utf-8")
    wrote_program = False
    if program and (force or not program_path.exists()):
        program_path.write_text(text, encoding="utf-8")
        wrote_program = True
    gi = co.root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    local = cfg.parent.name  # .benchbook, or .assay in a checkout set up before the rename
    add = [x for x in (f"{local}/ar_state.json", f"{local}/ar_pending.jsonl", f"{local}/ar_pending.jsonl.*",
                       "run.log", "ar_run.out") if x not in lines]
    if add:
        with open(gi, "a", encoding="utf-8") as f:
            if lines and lines[-1].strip():
                f.write("\n")
            f.write("\n".join(add) + "\n")
    hw_class = hwmod.detect(hw)
    part = _existing_partition_first(lg, co.partition_keys(hw_class))[0]
    return {"project": p.id, "created": created, "partition": part, "hw": hw_class,
            "config": str(cfg), "program": str(program_path), "wrote_program": wrote_program,
            "program_differs": (not wrote_program and program_path.exists()
                                and program_path.read_text(encoding="utf-8") != text),
            "agent_suggestion": localcfg.env("AGENT") or default_agent_name(),
            "prompt": "Read program.benchbook.md and start. Use the benchbook ar commands for every run."}


# ---------------------------------------------------------------------------------------------- pending

@contextmanager
def _queue_lock(p: Path, blocking: bool = True) -> Iterator[bool]:
    """Lock a stable file, independent of the journal being rotated."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a+b") as f:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError:
                if blocking:
                    raise
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                if blocking:
                    raise
                yield False
                return
        yield True  # closing the file releases the lock


def _append_line(p: Path, line: str) -> None:
    with _queue_lock(p.with_name(p.name + ".write.lock")):
        with p.open("ab") as f:
            f.write((line.rstrip("\n") + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())


def _pending_append(ctx: Ctx, record: dict[str, Any]) -> None:
    _append_line(ctx.co.pending_path(), json.dumps(record, default=str))


def _take_pending(p: Path) -> list[Path]:
    """Rotate while no producer holds an open descriptor on the active queue."""
    with _queue_lock(p.with_name(p.name + ".write.lock")):
        dst = p.with_name(f"{p.name}.{uuid4().hex}.flushing")
        try:
            os.replace(p, dst)
        except FileNotFoundError:
            return []
        return [dst]


def _persist_run(lg: Ledger, item: dict[str, Any], *, replay: bool = False) -> RecordResult:
    record = item["record"]
    stored = []
    if replay:
        # The connection may have failed after committing, or after recording but before linking/completing.
        stored = lg.sql_internal(
            "SELECT id, outcome, dedup_key, primary_value FROM benchbook.experiments WHERE project_id=%s"
            " AND task_id IS NOT DISTINCT FROM %s::uuid AND trace_ref->>'ar_run_id'=%s",
            (lg.project_id, record.get("task_id"), record["trace_ref"]["ar_run_id"]))
    if stored:
        r = RecordResult(**stored[0], duplicate_of=None, is_new_best=stored[0]["outcome"] == "improved")
    else:
        r = lg.record_experiment(**record)
    if r.id is not None and item.get("parent_id"):
        lg.link(r.id, "derived_from", item["parent_id"])
    if c := item.get("complete"):
        try:
            lg.complete_task(c["task"], "done", str(c.get("summary") or "")[:1000])
        except LeaseLost:
            pass  # a later claimant or a previous replay already ended this lease
    return r


def _save_or_queue(ctx: Ctx, record: dict[str, Any], summary: str,
                   parent_id: UUID | None = None) -> RecordResult | None:
    lg = ctx.lg
    item = {"agent": lg.agent, "project": lg.project_id, "record": record, "parent_id": parent_id,
            "complete": {"task": record["task_id"], "summary": summary}}
    try:
        return _persist_run(lg, item)
    except Exception as e:  # noqa: BLE001
        _pending_append(ctx, item)
        print(f"warning: could not finish saving the run ({e}); queued in {ctx.co.pending_path()}", file=sys.stderr)
        return None


def flush_pending(ctx: Ctx) -> int:
    """Record runs that finished while the ledger was unreachable, then complete their task or intent.
    Returns how many were written. Lines that still fail go back to the queue."""
    p = ctx.co.pending_path()
    if not p.parent.exists():
        return 0
    with _queue_lock(p.with_name(p.name + ".flush.lock"), blocking=False) as locked:
        if not locked:
            return 0
        done = 0
        # Any existing batch belongs to a process that no longer holds the lock.
        batches = sorted(p.parent.glob(p.name + ".*.flushing")) + _take_pending(p)
        for batch in batches:
            malformed = False
            try:
                lines = batch.read_bytes().split(b"\n")
            except OSError as e:
                print(f"warning: could not read pending run batch {batch}: {e}", file=sys.stderr)
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    malformed = True
                    continue
                if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
                    malformed = True
                    continue
                if item.get("project", ctx.lg.project_id) != ctx.lg.project_id:
                    _append_line(p, line.decode())
                    continue
                try:
                    lg = ctx.lg if item.get("agent") in (None, ctx.lg.agent) else ctx.lg.as_agent(item["agent"])
                    record = item["record"]
                    trace = record.get("trace_ref") or {}
                    trace.setdefault("ar_run_id", hashlib.sha256(line).hexdigest())
                    record["trace_ref"] = trace
                    _persist_run(lg, item, replay=True)
                except Exception:  # noqa: BLE001 - keep it for the next command
                    _append_line(p, line.decode())
                    continue
                done += 1
            if malformed:
                invalid = batch.with_suffix(".invalid")
                batch.replace(invalid)
                print(f"warning: saved malformed pending run data in {invalid}", file=sys.stderr)
            else:
                batch.unlink()
        return done


# ---------------------------------------------------------------------------------------------- reads

def experiments_for_code(ctx: Ctx, refs: tuple[str, ...], partition: str | None = None) -> list[dict[str, Any]]:
    return ctx.lg.sql_internal(
        "SELECT * FROM benchbook.experiments_valid WHERE project_id=%s AND code_ref=ANY(%s)"
        " AND config->>'partition'=ANY(%s)"
        " ORDER BY coalesce(seed,0), created_at, id",
        (ctx.lg.project_id, list(refs), [partition] if partition else list(ctx.partitions)))


def _finished_runs(ctx: Ctx, src: bytes, refs: tuple[str, ...]) -> list[dict[str, Any]]:
    # The stored blob URI also matches identical bytes from a host whose old ast.unparse renders them differently.
    # Runs recorded before the rename carry the same content under the assay:// scheme.
    blob, legacy_blob = blob_uris(hashlib.sha256(src).hexdigest())
    return ctx.lg.sql_internal(
        "SELECT id, config, code_ref, author, run_status, outcome, primary_value,"
        " v.id IS NOT NULL AS valid FROM benchbook.experiments e LEFT JOIN"
        " (SELECT id FROM benchbook.experiments_valid WHERE project_id=%(p)s) v USING (id)"
        " WHERE e.project_id=%(p)s AND e.config->>'partition'=ANY(%(parts)s)"
        " AND (e.code_ref=ANY(%(refs)s) OR e.artifacts @> %(blob)s OR e.artifacts @> %(legacy_blob)s)"
        " ORDER BY (e.run_status='ok') DESC, coalesce(e.seed,0), e.created_at, e.id",
        {"p": ctx.lg.project_id, "parts": list(ctx.partitions), "refs": list(refs), "blob": Jsonb([{"uri": blob}]),
         "legacy_blob": Jsonb([{"uri": legacy_blob}])})


def partition_best(ctx: Ctx, statuses: tuple[str, ...] = ("verified",)) -> dict[str, Any] | None:
    rows = ctx.lg.sql_internal(
        "SELECT * FROM benchbook.v_partition_best WHERE project_id=%s AND partition=ANY(%s)"
        " AND rank=1 AND status = ANY(%s)",
        (ctx.lg.project_id, list(ctx.partitions), list(statuses)))
    rows = [r for r in rows if r["mean"] is not None]
    if not rows:
        return None
    rows.sort(key=lambda r: r["mean"] if ctx.lg.project.metric_direction == "min" else -r["mean"])
    return rows[0]


def inflight(ctx: Ctx, others_only: bool = True) -> list[dict[str, Any]]:
    return ctx.lg.sql_internal(
        "SELECT * FROM benchbook.v_inflight WHERE project_id=%s AND is_intent AND partition=ANY(%s)"
        " AND (NOT %s OR agent <> %s) ORDER BY created_at",
        (ctx.lg.project_id, list(ctx.partitions), others_only, ctx.lg.agent))


def blob_of(exp: dict[str, Any]) -> str | None:
    return artifact_blob(exp.get("artifacts"))


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
        raise BenchbookError("describe the idea in one line")
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
            "SELECT id, author, outcome, primary_value, config->'changed' AS changed FROM benchbook.experiments_valid"
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
    previous = [t for t in ctx.lg.my_intents() if not t.spec.get("code_key")]
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
    refs = code_hashes(src)
    code_ref = refs[0]
    knobs = extract_knobs(src)
    settings = lg.refresh().settings
    ignore = settings.get("knob_ignore", DEFAULT_KNOB_IGNORE)
    parent_refs, parent_knobs, how = co.parent(src)
    if set(refs).intersection(parent_refs):  # nothing changed relative to the parent (e.g. a baseline re-run)
        parent_refs, parent_knobs = (), None
    parent_hash = parent_refs[0] if parent_refs else None
    config = {"partition": ctx.partition, "knobs": knobs,
              "changed": changed_knobs(parent_knobs, knobs, ignore) if parent_knobs is not None else {},
              "parent": parent_hash, "description": description[:500]}
    # ② finished duplicate
    _refuse_finished(ctx, src, refs, retry_failed)
    # ③ intent (and ② running duplicate)
    lease = int(timeout) + 300
    legacy_ref = refs[0] if ctx.partition == ctx.partitions[0] else refs[-1]
    code_key = f"{ctx.partition}|{legacy_ref}"
    canonical_key = f"{ctx.partitions[0]}|{code_ref}"
    patch = {"partition": ctx.partition, "code_hash": code_ref, "code_key": code_key,
             "canonical_code_key": canonical_key, "description": description, "running": True}
    prior_keys = {f"{p}|{h}" for p in ctx.partitions for h in refs}
    for existing in inflight(ctx, others_only=False):
        if existing["spec"].get("code_key") in prior_keys:
            raise Refused(f"DUPLICATE: the same code is running right now: {short('task', existing['id'])}")
    mine = [t for t in lg.my_intents() if not t.spec.get("code_key")]
    exclusive = ["code_key", "canonical_code_key"]
    try:
        if mine:
            intent = lg.update_intent(mine[0].id, patch, lease_seconds=lease, exclusive=exclusive)
        else:
            intent = lg.create_intent(description, {**patch, "auto": True}, lease_seconds=lease,
                                      exclusive=exclusive)
    except Conflict as e:
        raise Refused(f"DUPLICATE: the same code is running right now: {e}. Not run.", {"running": str(e)}) from e
    try:
        _refuse_finished(ctx, src, refs, retry_failed)
    except Refused:
        lg.complete_task(intent.id, "failed", "duplicate finished while starting")
        raise
    baseline, eps = lg.baseline(ctx.partition)
    uri = lg.put_blob(src, "text/x-python")
    # Resolve parent evidence before training. Once there is a result, no ledger read may bypass saving it.
    parents = experiments_for_code(ctx, parent_refs) if parent_refs else []
    parent_value = _mean_value(parents)
    parent_exp = parents[0]["id"] if parents else None
    run_id = uuid4().hex
    # ④⑤ run the hashed bytes (not the working file, which the agent may edit meanwhile) and parse
    res, interrupted = _run_isolated(ctx, src, cmd, timeout, parse_regex or DEFAULT_PARSE_REGEX, co.root / "run.log",
                                     {"BENCHBOOK_AR_RUN": "run", "BENCHBOOK_AR_SEED": "0",
                                      "BENCHBOOK_AR_CODE": code_ref, "BENCHBOOK_AR_RUN_ID": run_id})
    notes = description[:2000]
    if res.run_status != "ok":
        notes = f"{description}\n[{res.reason}] run.log tail:\n{res.log_tail}"[:2000]
    value = res.metrics.get(METRIC) if res.run_status == "ok" else None
    # A1: KEEP means better than this agent's parent; the server's outcome (against the verified best)
    # decides only whether the run is a candidate for replication.
    beats_parent = None if value is None or parent_value is None else better(lg, float(value), parent_value, eps)
    keep = value is not None and beats_parent is not False
    record: dict[str, Any] = {"config": config, "metrics": res.metrics if res.run_status == "ok" else
                              _numeric(res.metrics),
              "run_status": res.run_status, "seed": 0, "code_ref": code_ref, "notes": notes,
              "artifacts": [{"uri": uri, "kind": "train.py", "sha256": hashlib.sha256(src).hexdigest(),
                             "bytes": len(src)}],
              "task_id": str(intent.id), "duration_ms": res.duration_ms,
              "trace_ref": {"cmd": cmd, "parse_regex": parse_regex, "timeout": timeout,
                            "parent_value": parent_value, "beats_parent": beats_parent, "kept": keep,
                            "ar_run_id": run_id}}
    # ⑥⑦ persist the result before completing its intent. Retry any incomplete persistence on the next command.
    outcome0 = "failed" if res.run_status != "ok" else "queued"
    rec = _save_or_queue(ctx, record, f"{res.run_status} {METRIC}={value}", parent_exp)
    outcome = rec.outcome if rec is not None and rec.outcome else outcome0
    if rec is not None and rec.duplicate_of is not None:
        keep = False
    # ⑧ results.tsv (upstream format)
    head = co.git_show("HEAD") if co.is_git() else None
    commit = co.short_commit() if head is not None and code_hash(head) == code_ref else None
    tsv_status = "keep" if keep else ("crash" if res.run_status != "ok" else "discard")
    append_results_tsv(co.root / "results.tsv", results_tsv_line(
        commit or short_hash(code_ref, 7), value if tsv_status != "crash" else None,
        res.metrics.get("peak_vram_mb") if tsv_status != "crash" else None, tsv_status, description))
    # local state
    if rec is None or rec.id is not None:
        co.save_state(last_experiment=str(rec.id) if rec is not None else None,
                      last_code_hash=code_ref if keep else parent_hash, last_code_hashes=refs if keep else parent_refs,
                      last_knobs=knobs if keep else parent_knobs)
    if interrupted is not None:
        raise interrupted
    # ⑨ referee
    actions = safe_tick(lg) if rec is not None else []
    if rec is not None and rec.duplicate_of is not None:
        verdict = f"DUPLICATE (recorded meanwhile as {short('experiment', rec.duplicate_of)})"
    elif keep:
        if outcome == "queued":
            verdict = "KEEP (record queued)"
        elif outcome != "improved":
            verdict = "KEEP (local improvement)"
        elif baseline is None:
            verdict = "KEEP (baseline, calibration pending)"
        else:
            verdict = "KEEP (candidate, replication pending)"
    elif outcome == "improved" and parent_value is not None:
        verdict = (f"DISCARD (not better than its parent {parent_value:.6f} by epsilon {eps:.6f}; it still beats "
                   "the verified best, so the referee may replicate it)")
    elif outcome == "no_change":
        verdict = f"DISCARD (no change: within epsilon {eps:.6f} of {baseline:.6f})" if baseline is not None \
            else "DISCARD (no change)"
    elif outcome == "worse":
        verdict = f"DISCARD (worse by {abs(value - baseline):.6f})" if baseline is not None and value is not None \
            else "DISCARD (worse)"
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


def _refuse_finished(ctx: Ctx, src: bytes, refs: tuple[str, ...], retry_failed: bool) -> None:
    rows = _finished_runs(ctx, src, refs)
    if not rows or (retry_failed and rows[0]["run_status"] != "ok"):
        return
    prev = rows[0]
    ctx.lg.check_tried(prev["config"], prev["code_ref"])
    val = "" if prev["primary_value"] is None else f" {METRIC}={prev['primary_value']:.6f}"
    why = " (the prior run was invalidated)" if not prev["valid"] else ""
    retry = " (or pass --retry-failed to retry a failed run)" if prev["run_status"] != "ok" else ""
    raise Refused(f"DUPLICATE: this code already ran in {prev['config']['partition']}: "
                  f"{short('experiment', prev['id'])} by {prev['author']} -> {prev['outcome']}{val}{why}. "
                  f"Not run. Change train.py{retry}.",
                  {"duplicate_of": short("experiment", prev["id"]), "outcome": prev["outcome"],
                   "value": prev["primary_value"]})


def code_value(ctx: Ctx, refs: tuple[str, ...]) -> float | None:
    """Mean val_bpb over the successful runs of this code in the partition (first run and replications)."""
    return _mean_value(experiments_for_code(ctx, refs))


def _mean_value(rows: list[dict[str, Any]]) -> float | None:
    vals = [float(r["primary_value"]) for r in rows if r["run_status"] == "ok" and r["primary_value"] is not None]
    return statistics.fmean(vals) if vals else None


def time_budget(ctx: Ctx, harness: bytes | None = None) -> tuple[float | None, float, float | None]:
    """(training budget s, slack s, wall-clock allowance s) for the validity check (A9). The budget is the
    time_budget_s setting, else TIME_BUDGET in prepare.py, else 300; time_budget_s 0 turns the check off.
    `harness` is the prepare.py an imported run used, when it is not the one in the checkout."""
    return budget_of(ctx.lg.project.settings,
                     harness if harness is not None else (ctx.co.root / "prepare.py").read_bytes())


def budget_of(s: dict[str, Any], harness: bytes) -> tuple[float | None, float, float | None]:
    budget = s.get("time_budget_s")
    if budget is None:
        tb = extract_knobs(harness).get("TIME_BUDGET")
        budget = tb if isinstance(tb, (int, float)) and not isinstance(tb, bool) else 300
    overhead = s.get("time_budget_overhead_s", 240)
    return (float(budget) or None, float(s.get("time_budget_slack_s", 30)),
            None if overhead is None else float(overhead))


def _run_isolated(ctx: Ctx, src: bytes, cmd: str, timeout: float, parse_regex: str, log_path: Path,
                  env: dict[str, str]) -> tuple[RunResult, BaseException | None]:
    """Run `src` as train.py in a temporary copy of the checkout. An interruption (Ctrl-C, SIGTERM, SIGHUP)
    kills the training, becomes an error result and is returned so the caller records the run first (A4)."""
    budget, slack, overhead = time_budget(ctx)
    try:
        groups = re.compile(parse_regex).groups
    except re.error as e:
        raise BenchbookError(f"invalid --parse-regex: {e}") from e
    if groups < 2:
        raise BenchbookError("--parse-regex needs capture groups for the metric name and value")
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
        res = check_budget(res, budget, slack, overhead)
    return res, interrupted


def _numeric(m: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in m.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def safe_tick(lg: Ledger) -> list[str]:
    try:
        return referee.tick(lg)
    except Exception as e:  # noqa: BLE001 - the run is recorded; the next tick catches up
        print(f"warning: referee tick failed ({e}); the next benchbook ar command retries", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------------------------- replicate

def _isolated_dir(co: Checkout, src: bytes) -> Path:
    """§8.1: a temp dir with symlinks to every top-level entry of the checkout except train.py (and
    git, logs, results), plus the code under replication as train.py."""
    tmp = Path(tempfile.mkdtemp(prefix="benchbook-replicate-"))
    skip = {"train.py", ".git", "run.log", "results.tsv", localcfg.CONFIG_DIR.name, localcfg.LEGACY_CONFIG_DIR.name}
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
        for part in dict.fromkeys((ctx.partition, *ctx.partitions)):
            t = lg.next_task(tags=["replicate", "calibration"], lease_seconds=int(timeout) + 300,
                             spec_match={"partition": part})
            if t is not None:
                break
        if t is None:
            break
        spec = t.spec
        origin = lg.get(str(spec["origin"]))["record"]
        uri = str(spec.get("code_blob"))
        try:
            src = lg.get_blob(uri)
        except NotFound as e:
            lg.complete_task(t.id, "failed", f"code blob missing: {e}"[:1000])
            continue
        legacy_blob = (spec["partition"] != ctx.partitions[0]
                       and spec.get("code_ref") == origin.get("code_ref") and uri == blob_of(origin)
                       and blob_digest(uri) == hashlib.sha256(src).hexdigest())
        if spec.get("code_ref") not in code_hashes(src) and not legacy_blob:
            lg.complete_task(t.id, "failed", "code blob does not match code_ref")
            continue
        if co.partition(ctx.hw) != ctx.partitions[0] or spec.get("partition") not in ctx.partitions:
            lg.complete_task(t.id, "failed", "prepare.py differs from the task's harness; not run")
            continue
        run_cmd = str(spec.get("cmd") or cmd)
        regex = spec.get("parse_regex") or parse_regex or DEFAULT_PARSE_REGEX
        tmp_log = Path(tempfile.mkdtemp(prefix="benchbook-replicate-log-")) / "run.log"
        run_id = uuid4().hex
        try:
            res, interrupted = _run_isolated(ctx, src, run_cmd, timeout, regex, tmp_log,
                                             {"BENCHBOOK_AR_RUN": str(spec.get("kind") or "replicate"),
                                              "BENCHBOOK_AR_SEED": str(spec.get("seed")),
                                              "BENCHBOOK_AR_CODE": str(spec["code_ref"]),
                                              "BENCHBOOK_AR_RUN_ID": run_id})
        finally:
            shutil.rmtree(tmp_log.parent, ignore_errors=True)
        notes = f"replication {spec.get('seed')} of {short('experiment', origin['id'])}"
        if res.run_status != "ok":
            notes = f"{notes}\n[{res.reason}] run.log tail:\n{res.log_tail}"[:2000]
        record: dict[str, Any] = {"config": origin["config"], "metrics": res.metrics if res.run_status == "ok" else
                  _numeric(res.metrics), "run_status": res.run_status, "seed": int(spec["seed"]),
                  "code_ref": spec["code_ref"], "notes": notes, "artifacts": origin.get("artifacts") or [],
                  "task_id": str(t.id), "duration_ms": res.duration_ms,
                  "trace_ref": {"cmd": run_cmd, "parse_regex": regex, "timeout": timeout, "ar_run_id": run_id}}
        value = res.metrics.get(METRIC) if res.run_status == "ok" else None
        rec = _save_or_queue(ctx, record, f"{res.run_status} {METRIC}={value}")
        if interrupted is not None:
            raise interrupted
        actions = safe_tick(lg) if rec is not None else []
        done.append({"task": short("task", t.id), "kind": spec.get("kind"), "origin": short("experiment", origin["id"]),
                     "seed": spec.get("seed"), "run_status": res.run_status, "reason": res.reason, "value": value,
                     "id": short("experiment", rec.id) if rec is not None and rec.id else None,
                     "queued": rec is None, "duration_s": round(res.duration_ms / 1000, 1), "referee": actions})
    return done


# ---------------------------------------------------------------------------------------------- sync

def _bad_ancestor(ctx: Ctx, refs: tuple[str, ...]) -> str | None:
    """A refuted or inconclusive result on this code or any of its ancestors."""
    rows = experiments_for_code(ctx, refs)
    seen: set[UUID] = set()
    frontier = [r["id"] for r in rows if (r["seed"] or 0) == 0]
    while frontier:
        eid = frontier.pop()
        if eid in seen:
            continue
        seen.add(eid)
        bad = ctx.lg.sql_internal(
            "SELECT s.status FROM benchbook.links l JOIN benchbook.v_claim_status s ON s.id = l.dst_id"
            " WHERE l.project_id=%s AND l.src_id=%s AND l.relation='supports' AND s.kind='result'"
            " AND s.status IN ('refuted','inconclusive')", (ctx.lg.project_id, eid))
        if bad:
            return f"{short('experiment', eid)} was {bad[0]['status']}"
        frontier += [r["parent_id"] for r in ctx.lg.sql_internal(
            "SELECT parent_id FROM benchbook.v_lineage WHERE project_id=%s AND id=%s AND parent_id IS NOT NULL",
            (ctx.lg.project_id, eid))]
    return None


def sync(ctx: Ctx, trust: str = "verified") -> dict[str, Any]:
    if trust not in ("verified", "candidate"):
        raise BenchbookError("--trust must be verified or candidate")
    lg, co = ctx.lg, ctx.co
    target = partition_best(ctx, ("verified", "candidate") if trust == "candidate" else ("verified",))
    if target is None:
        return {"adopted": False, "reason": f"no {trust} result in {ctx.partition} yet", "partition": ctx.partition}
    src = co.train_bytes()
    mine = code_hashes(src)
    if target["code_ref"] in mine:
        return {"adopted": False, "reason": "you already have it", "code": short_hash(mine[0]),
                "value": target["mean"], "partition": ctx.partition}
    my_value = code_value(ctx, mine)
    bad = _bad_ancestor(ctx, mine)
    if my_value is not None and not better(lg, float(target["mean"]), my_value) and bad is None:
        return {"adopted": False, "reason": f"your code ({my_value:.6f}) is at least as good", "partition":
                ctx.partition, "mine": my_value, "target": target["mean"]}
    origin = lg.get(str(target["origin_id"]))["record"]
    uri = blob_of(origin)
    if uri is None:
        raise BenchbookError(f"the {target['status']} best has no stored code")
    code = lg.get_blob(uri)
    if code_hash(code) == mine[0]:
        return {"adopted": False, "reason": "you already have it", "code": short_hash(mine[0]),
                "value": target["mean"], "partition": ctx.partition}
    co.train_path.write_bytes(code)
    h8 = short_hash(target["code_ref"])
    committed = False
    if co.is_git():
        co.git("add", "train.py")
        msg = f"adopt {target['status']} best {h8} ({METRIC}={target['mean']:.6f})"
        r = co.git("commit", "-m", msg, "--", "train.py")
        committed = r is not None and r.returncode == 0
    co.save_state(last_code_hash=target["code_ref"], last_code_hashes=code_hashes(code), last_knobs=extract_knobs(code))
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

IMPORT_SOURCES = ("results.tsv", *skills.SKILLS)


def _results_tsv_rows(path: Path) -> list[skills.Row]:
    """Upstream results.tsv: commit, val_bpb, memory_gb, status, description. Every row names its commit."""
    import csv

    with open(path, encoding="utf-8", newline="") as fh:
        raw = list(csv.DictReader(fh, delimiter="\t"))
    rows = []
    for r in raw:
        status = (r.get("status") or "").strip().lower()
        try:
            val = float(r.get("val_bpb") or 0)
            mem = float(r.get("memory_gb") or 0)
        except ValueError:
            val, mem = 0.0, 0.0
        ok = status != "crash" and val > 0
        rows.append(skills.Row(key="", label="", status=status, run_status="ok" if ok else "error",
                               kept=status == "keep", value=val if ok else None,
                               description=(r.get("description") or "").strip(),
                               commit=(r.get("commit") or "").strip() or None,
                               metrics={"peak_vram_mb": mem * 1024} if ok else {}))
    return rows


def _imported_keys(lg: Ledger, keys: list[str], hw_class: str) -> set[str]:
    """Skill records this hardware class already holds, invalidated ones included: importing is append-only."""
    rows = lg.sql_internal(
        "SELECT trace_ref->>'import_key' AS k FROM benchbook.experiments WHERE project_id=%s"
        " AND trace_ref->>'hw'=%s AND trace_ref->>'import_key'=ANY(%s)", (lg.project_id, hw_class, keys))
    return {r["k"] for r in rows}


def import_results(ctx: Ctx, path: Path, hw: str | None = None, source: str = "results.tsv",
                   metric: str | None = None) -> dict[str, Any]:
    """Record runs that happened outside `benchbook ar run`: an upstream results.tsv, or the log of one of the
    autoresearch skills (`source`, see skills.py). Each row's train.py comes from `git show <commit>:train.py`.

    Imported runs are first runs, always replication number 0: an improvement becomes a candidate and other agents
    re-run it before it counts, and nothing in a log can stand in for one of those re-runs (the ledger keeps one
    ok run per code and replication number, and the rerun rule needs three numbers from two authors).
    `hw` is the hardware class the old runs used and must be given (A9): no log says which GPU produced it.
    The evaluator version is the hash of prepare.py at each row's commit: it names the partition, so it is part
    of the row's identity next to code_ref. The time budget is checked as for a live run, on what the log holds.
    `metric` is the name the log uses for the project's primary metric when it differs."""
    lg, co = ctx.lg, ctx.co
    if source not in IMPORT_SOURCES:
        raise BenchbookError(f"--from must be one of: {', '.join(skills.SKILLS)} (leave it out for results.tsv)")
    skill = source != "results.tsv"
    if skill:
        path = skills.locate(source, path)
    elif not path.is_file():
        raise BenchbookError(f"no such file: {path}")
    if not hw:
        raise BenchbookError(f"pass --hw with the hardware class the imported runs used (this machine: {ctx.hw}); "
                             "results from other GPUs must not enter this machine's partition")
    hw_class = hwmod.slug(hw)
    if not co.is_git():
        raise BenchbookError("benchbook ar import needs the git history of this checkout")
    if held := lg.live_leases():
        # record_experiment attaches a run recorded without a task to the one task its author holds: an import
        # made while this agent holds a replication task would be counted as that replication's result.
        raise BenchbookError(f"{lg.agent} holds a live lease on {short('task', held[0].id)}; an imported run would be "
                             "recorded as that task's result. Finish the task or wait for its lease to expire, "
                             "or import with --agent <another name>.")
    log = skills.read(source, path) if skill else skills.Log(_results_tsv_rows(path))
    counts = {"imported": 0, "duplicates": 0, "missing_commit": 0, "rows": 0}
    if skill:
        counts.update(other_metric=0, skipped=log.skipped)
    primary = lg.project.primary_metric or METRIC
    wanted = (metric or primary).lower()
    seen = _imported_keys(lg, [r.key for r in log.rows], hw_class) if skill else set()
    others: set[str] = set()
    known_parts: dict[tuple[str, ...], str] = {}
    ignore = ctx.settings.get("knob_ignore", DEFAULT_KNOB_IGNORE)
    for row in log.rows:
        counts["rows"] += 1
        if row.key in seen:
            counts["duplicates"] += 1
            continue
        if ((row.metric is not None and row.metric.lower() != wanted)
                or (row.direction is not None and row.direction != lg.project.metric_direction)):
            counts["other_metric"] += 1  # a pi segment or a codex run that optimised something else
            others.add(f"{row.metric or 'its metric'} ({row.direction or 'direction not stated'})")
            continue
        src = co.git_show(row.commit) if row.commit else None
        if src is None and (row.commit or not skill):
            counts["missing_commit"] += 1
            continue
        at = row.commit or row.parent_commit  # where this row's prepare.py, its evaluator, is read
        harness = co.git_show(at, "prepare.py") if at else None
        parts = partition_keys(hw_class, harness) if harness is not None else co.partition_keys(hw_class)
        if parts not in known_parts:
            known_parts[parts] = _existing_partition_first(lg, parts)[0]
        part = known_parts[parts]
        refs: tuple[str, ...] = ()
        code_ref: str | None = None
        knobs: dict[str, Any] = {}
        if src is not None:
            refs = code_hashes(src)
            code_ref = refs[0]
            knobs = extract_knobs(src)
            prev = co.git_show(f"{row.commit}~1")
        else:
            prev = co.git_show(row.parent_commit) if row.parent_commit else None
        parent_hash = code_hash(prev) if prev is not None else None
        if parent_hash == code_ref:
            parent_hash, prev = None, None
        description = row.description.strip()[:500]
        changed = changed_knobs(extract_knobs(prev), knobs, ignore) if prev is not None and src is not None else {}
        config: dict[str, Any] = {"partition": part, "knobs": knobs, "changed": changed, "parent": parent_hash,
                                  "description": description}
        if src is not None and any(f"commit {row.commit}," in (e["notes"] or "")
                                   for p in dict.fromkeys((part, *parts)) for e in experiments_for_code(ctx, refs, p)):
            counts["duplicates"] += 1  # imported before (failed rows are not caught by the dedup index)
            continue
        ok = row.run_status == "ok" and row.value is not None
        run_status = "ok" if ok else ("invalid" if row.run_status == "invalid" else "error")
        metrics = {**row.metrics, primary: row.value} if row.value is not None and run_status != "error" else {}
        why = ""
        if ok and src is None:
            # A measured change the skill reverted without committing it. Its value cannot be re-run or tied to
            # code, so it must not become a candidate or the best that later runs are compared with; it stays on
            # record (value, description) as an approach that was tried.
            run_status, why = "invalid", " [no code: the skill reverted this change without committing it]"
        elif ok:
            # No log records the wall clock of a run, so only the training time it reports can be checked.
            budget, slack, _ = time_budget(ctx, harness)
            checked = check_budget(RunResult("ok", 0, dict(metrics)), budget, slack)
            if checked.run_status != "ok":
                run_status, why = checked.run_status, f" [{checked.reason}]"
        evaluator = parts[0].rsplit("/", 1)[-1]
        if skill:
            where = ", ".join(x for x in (row.label, f"commit {row.commit}" if row.commit else "", row.status) if x)
            notes = f"{description} (imported from {source}, {where}){why}"
            trace = {"imported_from": source, "file": path.name, "import_key": row.key, "commit": row.commit,
                     "hw": hw_class, "status": row.status, "kept": row.kept, "evaluator": evaluator, **row.detail}
        else:
            notes = f"{description} (imported from results.tsv, commit {row.commit}, {row.status}){why}"
            trace = {"imported_from": path.name, "commit": row.commit, "hw": hw_class, "evaluator": evaluator}
        artifacts = []
        if src is not None:
            artifacts = [{"uri": lg.put_blob(src, "text/x-python"), "kind": "train.py",
                          "sha256": hashlib.sha256(src).hexdigest(), "bytes": len(src)}]
        rec = lg.record_experiment(config, metrics, run_status=run_status, seed=0, code_ref=code_ref,
                                   notes=notes[:2000], artifacts=artifacts, trace_ref=trace)
        if rec.duplicate_of is not None or rec.id is None:
            counts["duplicates"] += 1
            continue
        counts["imported"] += 1
        if parent_hash:
            parents = experiments_for_code(ctx, code_hashes(prev), part) if prev is not None else []
            if parents:
                lg.link(rec.id, "derived_from", parents[0]["id"])
    if others and counts["other_metric"] == counts["rows"]:
        raise BenchbookError(
            f"{path} measures {', '.join(sorted(others))}; this project measures {primary} "
            f"({lg.project.metric_direction}). Pass --metric <name> if the log's metric is {primary} under another "
            "name. A log that optimises in the other direction cannot be imported.")
    counts["referee"] = len(safe_tick(lg))
    return counts


# ---------------------------------------------------------------------------------------------- status

# B5: a duplicate counts as blocked only when it was refused before running: 'avoided' hits that the same agent
# did not follow with a run of that code anyway (--retry-failed). 'rejected' hits are runs that already
# happened (a race, or a queued run replayed) and are not blocked runs.
BLOCKED_SQL = (
    "SELECT count(*) AS n, coalesce(sum(e.duration_ms),0)/60000.0 AS min FROM benchbook.dedup_hits h"
    " JOIN benchbook.experiments e ON e.id = h.existing_id WHERE h.project_id=%s AND e.config->>'partition'=%s"
    " AND h.kind='avoided' AND NOT EXISTS (SELECT 1 FROM benchbook.experiments x WHERE x.project_id=h.project_id"
    "   AND x.dedup_key=h.dedup_key AND x.author=h.agent_id AND coalesce(x.seed,0)=0"
    "   AND x.created_at >= h.created_at)")


def status(lg: Ledger, partition: str | None = None) -> dict[str, Any]:
    pid = lg.project_id
    lg.refresh()
    parts = [r["p"] for r in lg.sql_internal(
        "SELECT DISTINCT config->>'partition' AS p FROM benchbook.experiments_valid WHERE project_id=%s"
        " AND config->>'partition' IS NOT NULL ORDER BY 1", (pid,))]
    if partition:
        parts = [p for p in parts if p == partition] or [partition]
    cal = lg.project.settings.get("partitions") or {}
    out = []
    for p in parts:
        best = lg.sql_internal("SELECT * FROM benchbook.v_partition_best WHERE project_id=%s AND partition=%s"
                               " AND rank=1 AND status='verified'", (pid, p))
        cands = lg.sql_internal("SELECT * FROM benchbook.v_partition_best WHERE project_id=%s AND partition=%s"
                                " AND status='candidate' ORDER BY rank", (pid, p))
        fly = lg.sql_internal("SELECT agent, title, lease_left_s, spec->'knobs' AS knobs, id FROM benchbook.v_inflight"
                              " WHERE project_id=%s AND partition=%s AND is_intent ORDER BY created_at", (pid, p))
        queue = lg.sql_internal(
            "SELECT status, count(*) AS n FROM benchbook.tasks WHERE project_id=%s AND spec->>'partition'=%s"
            " AND tags && ARRAY['replicate','calibration'] GROUP BY status", (pid, p))
        gpu = lg.sql_internal(
            "SELECT coalesce(sum(duration_ms),0)/60000.0 AS total_min,"
            " coalesce(sum(duration_ms) FILTER (WHERE coalesce(seed,0) > 0),0)/60000.0 AS replicate_min,"
            " count(*) AS runs, count(*) FILTER (WHERE run_status <> 'ok') AS failed"
            " FROM benchbook.experiments WHERE project_id=%s AND config->>'partition'=%s", (pid, p))[0]
        blocked = lg.sql_internal(BLOCKED_SQL, (pid, p))[0]
        verdicts = {r["status"]: r["n"] for r in lg.sql_internal(
            "SELECT status, count(*) AS n FROM benchbook.v_claim_status s JOIN benchbook.claims c ON c.id=s.id"
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
