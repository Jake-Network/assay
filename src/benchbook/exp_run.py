"""`benchbook exp run`: check, run and record one evaluation in a single command.

Each attempt used to cost an agent three tool calls (check_tried, the evaluation, record_experiment), and every
call is a turn that re-reads the context. Here the ledger is asked before the command starts and the result is
recorded when it ends, so the agent types one command. The guarantees follow `benchbook ar run`: an exact earlier
run refuses the attempt before anything runs, and every command that starts is recorded, whether it exits non-zero,
times out or is interrupted."""
from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from .checks.runner import Interrupted, RunResult, parse_summary, run_command, tail
from .core.ids import short

if TYPE_CHECKING:
    from .core.ledger import Ledger
    from .core.models import ExperimentSummary

TIMEOUT_EXIT = 124  # what timeout(1) exits with when it kills the command
RECEIPT = "benchbook: "  # the start of every line exp run adds to stdout
NOTES_MAX = 2000
# Keys of an output JSON object that describe the attempt rather than measure it (the bench wrapper's receipt).
_NOT_METRICS = frozenset({"seed", "run_status", "notes", "invalid", "value", "metrics"})


@dataclass
class Parsed:
    """What the command's output says about the attempt."""

    metrics: dict[str, float | int] = field(default_factory=dict)
    run_status: str | None = None  # a run_status the output names (evaluator.py: "invalid")
    note: str | None = None


def command_line(args: list[str]) -> str:
    """One argument is a shell command line as written (`-- 'make eval && cat score'`); several are quoted and joined,
    so `-- python evaluator.py my file.py` cannot be split differently by the shell."""
    if len(args) == 1:
        return args[0]
    return subprocess.list2cmdline(args) if sys.platform == "win32" else shlex.join(args)


def _number(v: Any) -> float | int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v if math.isfinite(v) else None


def parse_output(text: str, primary: str | None = None) -> Parsed:
    """The two formats the examples already print. `key: number` lines anywhere (runner.parse_summary, the
    autoresearch summary), and a JSON object as the last line (examples/circle-packing/evaluator.py, bench's
    tools/evaluate.py). From the object: every top-level number (or those in a "metrics" object) is a metric;
    "run_status", "invalid" (a reason, so run_status invalid) and "notes" describe the attempt, and "value" is the
    primary metric when the object does not name it. The object wins over summary lines with the same key."""
    from .core.ledger import RUN_STATUSES

    out = Parsed(metrics=parse_summary(text))
    lines = [x.strip() for x in text.replace("\r", "\n").splitlines() if x.strip()]
    if not lines or not lines[-1].startswith("{"):
        return out
    try:
        obj = json.loads(lines[-1])
    except ValueError:
        return out
    if not isinstance(obj, dict):
        return out
    src = obj["metrics"] if isinstance(obj.get("metrics"), dict) else obj
    for k, v in src.items():
        if (src is not obj or k not in _NOT_METRICS) and (n := _number(v)) is not None:
            out.metrics[str(k)] = n
    if primary and primary not in out.metrics and (n := _number(obj.get("value"))) is not None:
        out.metrics[primary] = n
    if obj.get("run_status") in RUN_STATUSES:
        out.run_status = obj["run_status"]
    if isinstance(obj.get("invalid"), str):
        out.run_status = out.run_status or "invalid"
        out.note = obj["invalid"]
    if isinstance(obj.get("notes"), str) and obj["notes"].strip():
        out.note = f"{out.note}; {obj['notes']}" if out.note else obj["notes"]
    return out


def _earlier_line(e: ExperimentSummary, invalidated: bool = False) -> str:
    value = "" if e.primary_value is None else f" {e.primary_value}"
    return (f"{short('experiment', e.id)} by {e.author} (seed {e.seed}): {e.run_status} {e.outcome}{value}"
            + (" (invalidated)" if invalidated else ""))


def _earlier_data(e: ExperimentSummary, invalidated: bool = False) -> dict[str, Any]:
    return {"id": short("experiment", e.id), "author": e.author, "seed": e.seed, "run_status": e.run_status,
            "outcome": e.outcome, "value": e.primary_value, "notes": e.notes, "invalidated": invalidated}


def _note(msg: str) -> None:
    print(f"note: {msg}", file=sys.stderr, flush=True)


def _check_first(lg: Ledger, config: dict[str, Any], code_ref: str | None, seed: int | None,
                 retry_failed: bool) -> None:
    """Ask the ledger before anything runs. A refuse-mode pack stops here (exit 2) when this config, code_ref and
    seed already ran: an ok run (invalidated ones too: the unique index still counts them, DECISIONS.md) can never
    be recorded again, and a failed one is not retried without --retry-failed. A notify-mode pack runs it anyway."""
    from .cli import STATE, emit

    tried = lg.check_tried(config, code_ref)  # also validates the config, so a bad one never runs
    same = [(e, False) for e in tried.exact if e.seed == seed]
    same += [(e, True) for e in tried.invalidated if e.seed == seed]
    ok_same = [x for x in same if x[0].run_status == "ok"]
    if same and lg.pack.dedup_mode != "notify" and (ok_same or not retry_failed):
        e, inval = (ok_same or same)[0]
        hint = ("Use a different seed to replicate, or change the config." if ok_same else
                "It failed before; pass --retry-failed to run it again, or change the config.")
        emit({"ok": False, "ran": False, "duplicate_of": short("experiment", e.id), "dedup_key": tried.dedup_key,
              "earlier": _earlier_data(e, inval), "note": tried.note},
             f"{RECEIPT}not run: duplicate of {_earlier_line(e, inval)}. Same config, code_ref and seed. {hint}")
        raise typer.Exit(2)
    if STATE["json"]:
        return  # the JSON result names the earlier run (duplicate_of); stderr stays quiet
    if same:
        e, inval = same[0]
        repeat = ("running it again (pack " + lg.pack.name + ": dedup_mode notify)" if lg.pack.dedup_mode == "notify"
                  else "it failed; running it again (--retry-failed)")
        _note(f"same config, code_ref and seed as {_earlier_line(e, inval)}; {repeat}")
    others = [e for e in tried.exact if e.seed != seed]
    if others:
        ids = ", ".join(f"{short('experiment', e.id)} (seed {e.seed}, {e.run_status})" for e in others[:3])
        _note(f"this config and code_ref ran {len(others)}x with other seeds, best {tried.best_value}: {ids}"
              + (", ..." if len(others) > 3 else ""))
    if tried.note:
        _note(tried.note)


def _run(cmd: str, timeout: float, env: dict[str, str]) -> tuple[RunResult, str, BaseException | None, Path]:
    """Run with tee so the agent sees the command's own output. An interruption kills the command, becomes an
    error result and is returned, so the caller records the run before it re-raises (as ar run does)."""
    fd, name = tempfile.mkstemp(prefix="benchbook-exp-run-", suffix=".log")
    os.close(fd)
    log = Path(name)
    t0 = time.monotonic()
    interrupted: BaseException | None = None
    try:
        res = run_command(cmd, Path.cwd(), log, timeout, env=env, tee=True)
    except (KeyboardInterrupt, Interrupted) as e:
        interrupted = e
        text = log.read_bytes().decode("utf-8", errors="replace")
        sig = e.args[0] if isinstance(e, Interrupted) else "SIGINT"
        res = RunResult("error", None, parse_summary(text), int((time.monotonic() - t0) * 1000), tail(text),
                        f"interrupted ({sig})")
    return res, log.read_bytes().decode("utf-8", errors="replace"), interrupted, log


def _exit_code(res: RunResult, interrupted: BaseException | None) -> int:
    if isinstance(interrupted, Interrupted):
        return 128 + interrupted.signum
    if interrupted is not None:
        return 130  # SIGINT, as a shell reports it
    if res.exit_code is None:
        return TIMEOUT_EXIT
    return res.exit_code


def exp_run(
    cmd: Annotated[list[str], typer.Argument(metavar="CMD", show_default=False,
                                             help="the evaluation command; put -- before it")],
    config: Annotated[str, typer.Option(help="JSON or @file")],
    seed: Annotated[int | None, typer.Option()] = None,
    code_ref: Annotated[str | None, typer.Option("--code-ref")] = None,
    notes: Annotated[str | None, typer.Option()] = None,
    task: Annotated[str | None, typer.Option()] = None,
    artifacts: Annotated[str | None, typer.Option(help="JSON list or @file")] = None,
    trace_ref: Annotated[str | None, typer.Option("--trace-ref", help="JSON object or @file")] = None,
    tokens: Annotated[int | None, typer.Option()] = None,
    cost_usd: Annotated[float | None, typer.Option("--cost-usd")] = None,
    timeout: Annotated[float, typer.Option(help="seconds before the command is killed")] = 600,
    retry_failed: Annotated[bool, typer.Option("--retry-failed",
                                               help="run again when this config, code_ref and seed only failed")]
    = False,
) -> None:
    """Check, run and record one evaluation, so one command replaces exp tried + exp record.

    benchbook exp run --config '{"lr": 3e-4}' --seed 0 -- python eval.py

    Before running: if this config, code_ref and seed already ran, a refuse-mode
    pack prints the earlier run and exits 2 without running (a run that only
    failed runs again with --retry-failed). A notify-mode pack runs it anyway and
    names the earlier run.

    The command runs through the shell in the current directory, stdin closed.
    Its stdout and stderr pass through unchanged on stdout. Its environment has
    BENCHBOOK_EXP_CONFIG (the config as JSON) and BENCHBOOK_EXP_SEED.

    Metrics come from its output: "key: number" lines, and a JSON object on the
    last line (numbers are metrics; "run_status", "invalid" and "notes" describe
    the attempt; "value" is the primary metric if the object does not name it).

    Every run is recorded, failures included: a non-zero exit is run_status
    error, the timeout is timeout, Ctrl-C/SIGTERM/SIGHUP is error (cause in notes).

    Output: after the command's output, one line on stdout that starts with
    "benchbook: ", e.g. "benchbook: recorded e:1a2b3c4d improved sum_radii=2.63
    (ok, 1.2s)". With --json that last line is a JSON object. Notes about
    earlier runs go to stderr.

    Exit code: the command's own; 124 after the timeout; 128+N after signal N
    (130 for Ctrl-C); 2 when a duplicate was not run, or not recorded because
    another agent recorded the same run meanwhile; 1 for a usage error or a run
    that could not be recorded.

    Without --task the experiment belongs to the one task you hold a lease on,
    if there is exactly one."""
    from .cli import STATE, die, emit, ledger, load_json

    line = command_line(cmd)
    if not line.strip():
        die("give the command to run after --")
    lg = ledger()
    cfg = load_json(config, "--config")
    trace = load_json(trace_ref, "--trace-ref")
    if trace is not None and not isinstance(trace, dict):
        die("--trace-ref: must be a JSON object")
    arts = load_json(artifacts, "--artifacts")
    if task is None:
        leases = lg.live_leases()
        if len(leases) > 1:
            print(f"warning: you hold {len(leases)} tasks ({', '.join(t.short_id for t in leases)}); pass --task so "
                  "this experiment shows up in the briefs of related tasks", file=sys.stderr)
    _check_first(lg, cfg, code_ref, seed, retry_failed)

    env = {**os.environ, "BENCHBOOK_EXP_CONFIG": json.dumps(cfg, ensure_ascii=False)}
    env.pop("BENCHBOOK_EXP_SEED", None)
    if seed is not None:
        env["BENCHBOOK_EXP_SEED"] = str(seed)
    res, text, interrupted, log = _run(line, timeout, env)

    parsed = parse_output(text, lg.project.primary_metric)
    status, reason = res.run_status, res.reason
    if status == "ok" and parsed.run_status not in (None, "ok"):
        status, reason = str(parsed.run_status), parsed.note or f"the output says run_status {parsed.run_status}"
    if status == "ok":
        missing = [m for m in lg.pack.ok_metrics_required if m not in parsed.metrics]
        if missing:
            status, reason = "error", f"no {', '.join(missing)} in the output"
    note = notes or ""
    if status == "ok" and parsed.note:
        note = f"{note}\n{parsed.note}" if note else parsed.note
    elif status != "ok":
        cause = f"[{reason}] output tail:\n{res.log_tail}"
        note = f"{note}\n{cause}" if note else cause
    code = _exit_code(res, interrupted)
    record: dict[str, Any] = {
        "config": cfg, "metrics": parsed.metrics, "run_status": status, "seed": seed, "code_ref": code_ref,
        "notes": note[:NOTES_MAX] or None, "task_id": task, "artifacts": arts,
        "trace_ref": {"cmd": line, "exit_code": res.exit_code, **(trace or {})}, "duration_ms": res.duration_ms,
        "tokens": tokens, "cost_usd": cost_usd}
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")  # the line exp run adds starts on its own line
    try:
        r = lg.record_experiment(**record)
    except Exception as e:  # noqa: BLE001 - the run happened: say so and keep its output
        die(f"the command ran ({status}, exit code {code}) but could not be recorded: {e}. Its output is in {log}; "
            f"record it with benchbook exp record --config '{json.dumps(cfg)}' ...")
    log.unlink(missing_ok=True)
    dup = short("experiment", r.duplicate_of) if r.duplicate_of is not None else None
    secs = f"{res.duration_ms / 1000:.1f}s"
    if r.id is None:  # another agent recorded the same ok run while this one ran (refuse mode)
        emit({"ok": False, "ran": True, "run_status": status, "exit_code": res.exit_code, "metrics": parsed.metrics,
              "duplicate_of": dup, "dedup_key": r.dedup_key},
             f"{RECEIPT}not recorded: duplicate of {dup}, recorded while this ran ({status}, {secs})")
        raise typer.Exit(2)
    rid = short("experiment", r.id)
    data: dict[str, Any] = {"ok": True, "ran": True, "id": rid, "outcome": r.outcome, "is_new_best": r.is_new_best,
                            "run_status": status, "exit_code": res.exit_code, "reason": reason or None,
                            "metrics": parsed.metrics, "duration_ms": res.duration_ms, "duplicate_of": dup}
    if r.notice is not None:
        data["notice"] = r.notice
        if not STATE["json"]:
            print(f"notice: {r.notice}", file=sys.stderr)
    pm = lg.project.primary_metric
    value = f" {pm}={r.primary_value}" if pm and r.primary_value is not None else ""
    emit(data, f"{RECEIPT}recorded {rid} {r.outcome}{value}{'  ★ new best' if r.is_new_best else ''} "
               f"({status}{': ' + reason if reason else ''}, {secs})" + (f", repeat of {dup}" if dup else ""))
    if code:
        raise typer.Exit(code)
