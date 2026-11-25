"""`benchbook ar`: autoresearch on many GPUs as one lab (§8). Exit codes: 0 ok, 1 user error,
2 refused (duplicate or conflict), 3 nothing to do."""
from __future__ import annotations

import time
from typing import Annotated, Any

import typer

from .gate import gate_app

ar_app = typer.Typer(no_args_is_help=True, help="autoresearch on many GPUs as one lab (run in the checkout root).")
ar_app.add_typer(gate_app, name="gate")

HwOpt = Annotated[str | None, typer.Option("--hw", help="hardware class override (else BENCHBOOK_AR_HW, nvidia-smi)")]
CmdOpt = Annotated[str, typer.Option("--cmd", help="training command")]
TimeoutOpt = Annotated[float, typer.Option("--timeout", help="seconds before the run is killed")]
RegexOpt = Annotated[str | None, typer.Option("--parse-regex", help="summary line regex: (key) (number)")]


def _st() -> dict[str, Any]:
    from ...cli import STATE

    return STATE


def _emit(data: Any, text: str) -> None:
    from ...cli import emit

    emit(data, text)


def _ctx(hw: str | None = None) -> Any:
    from .ops import open_ctx

    st = _st()
    return open_ctx(project=st["project"], agent=st["agent"], hw=hw)


def _refused(e: Exception) -> None:
    from ...cli import STATE, emit

    data = {"ok": False, "refused": True, "error": str(e), **getattr(e, "data", {})}
    if STATE["json"]:
        emit(data, "")
    else:
        print(str(e))
    raise typer.Exit(2)


def _knobs(values: list[str] | None) -> dict[str, Any]:
    from ...errors import BenchbookError
    from .code import KNOB_NAME, parse_knob_value

    out: dict[str, Any] = {}
    for item in values or []:
        k, sep, v = item.partition("=")
        k = k.strip()
        if not sep or not KNOB_NAME.match(k):
            raise BenchbookError(f"--knob expects NAME=VALUE with an UPPER_CASE name, got {item!r}")
        out[k] = parse_knob_value(v)
    return out


@ar_app.command("init")
def init(project: Annotated[str | None, typer.Option("--project", help="project id (default: autoresearch)")] = None,
         hw: HwOpt = None,
         force: Annotated[bool, typer.Option("--force", help="overwrite program.benchbook.md")] = False) -> None:
    """Attach benchbook to this autoresearch checkout: project, program.benchbook.md, .gitignore."""
    from ...config import load
    from .ops import init as do_init

    pid = project or _st()["project"] or load().project or "autoresearch"
    r = do_init(pid, hw=hw, force=force)
    lines = [f"{'created' if r['created'] else 'using existing'} project {r['project']} (pack ml-research)",
             f"  partition: {r['partition']}   (hardware class / prepare.py hash)",
             f"  config:    {r['config']}",
             f"  program:   {r['program']}" + ("" if r["wrote_program"] else
                                              "  (kept existing; --force to overwrite)"
                                              + ("  WARNING: differs from this benchbook version"
                                                 if r["program_differs"] else "")),
             "",
             "Per GPU (one agent each):",
             f"  export BENCHBOOK_AGENT={r['agent_suggestion']} BENCHBOOK_PROJECT={r['project']}"
             " CUDA_VISIBLE_DEVICES=0",
             "  claude",
             f"Prompt: \"{r['prompt']}\"",
             "Several GPUs on one machine: integrations/autoresearch/launch_local.sh (git worktree per GPU).",
             "Several machines: the same BENCHBOOK_DATABASE_URL (Postgres) and BENCHBOOK_PROJECT everywhere."]
    _emit({"ok": True, **r}, "\n".join(lines))


@ar_app.command("brief")
def brief(budget: Annotated[int, typer.Option("--budget")] = 1500, hw: HwOpt = None) -> None:
    """What is verified, pending, in progress and already tried in your partition."""
    ctx = _ctx(hw)
    b = ctx.lg.brief(budget_tokens=budget, partition=ctx.partition)
    _emit({"brief": b, "partition": ctx.partition}, b.rstrip("\n"))


@ar_app.command("claim")
def claim(description: str,
          knob: Annotated[list[str] | None, typer.Option("--knob", help="NAME=VALUE, repeatable")] = None,
          force: Annotated[bool, typer.Option("--force", help="claim even if a similar idea is in progress")] = False,
          hw: HwOpt = None) -> None:
    """Announce the idea you are about to try (15 min lease). Exit 2 if someone is already on it."""
    from .ops import Refused
    from .ops import claim as do_claim

    ctx = _ctx(hw)
    try:
        r = do_claim(ctx, description, _knobs(knob), force)
    except Refused as e:
        _refused(e)
        return
    lines = [f"{r['intent']} claimed for 15 min: {description}"] + [f"  warning: {w}" for w in r["warnings"]]
    _emit({"ok": True, **r}, "\n".join(lines))


@ar_app.command("run")
def run(message: Annotated[str, typer.Option("-m", "--message", help="one-line description (results.tsv)")],
        cmd: CmdOpt = "uv run train.py", timeout: TimeoutOpt = 600, parse_regex: RegexOpt = None,
        retry_failed: Annotated[bool, typer.Option("--retry-failed", help="re-run code whose only runs failed")]
        = False, hw: HwOpt = None) -> None:
    """Run train.py once through the ledger: refuse duplicates, record everything, ask the referee."""
    from .ops import Refused
    from .ops import run as do_run

    ctx = _ctx(hw)
    try:
        r = do_run(ctx, message, cmd, timeout, parse_regex, retry_failed)
    except Refused as e:
        _refused(e)
        return
    val = "" if r["value"] is None else f"  val_bpb={r['value']:.6f}"
    base = "" if r["baseline"] is None else f"  baseline={r['baseline']:.6f}"
    text = f"{r['verdict']}{val}{base}  epsilon={r['epsilon']:.6f}  {r['id'] or ''}  ({r['duration_s']}s)"
    _emit({"ok": True, **r}, text)


@ar_app.command("replicate")
def replicate(auto: Annotated[bool, typer.Option("--auto", help="take a replication task if one is waiting")]
              = False,
              max_: Annotated[int, typer.Option("--max", help="at most this many tasks")] = 1,
              cmd: CmdOpt = "uv run train.py", timeout: TimeoutOpt = 600, parse_regex: RegexOpt = None,
              hw: HwOpt = None) -> None:
    """Re-run someone else's candidate in an isolated copy (your train.py and git are untouched).
    Exit 3 when there is nothing to replicate."""
    from ...errors import BenchbookError
    from .ops import replicate as do_rep

    if not auto:
        raise BenchbookError("pass --auto (replication tasks are assigned by the referee)")
    ctx = _ctx(hw)
    done = do_rep(ctx, max_, cmd, timeout, parse_regex)
    if not done:
        _emit({"ok": True, "done": []}, "nothing to replicate")
        raise typer.Exit(3)
    lines = [f"{d['kind']} {d['seed']} of {d['origin']}: {d['run_status']}"
             + (f" val_bpb={d['value']:.6f}" if d["value"] is not None else "") + f" ({d['duration_s']}s)"
             for d in done]
    _emit({"ok": True, "done": done}, "\n".join(lines))


@ar_app.command("sync")
def sync(trust: Annotated[str, typer.Option("--trust", help="verified (default) or candidate")] = "verified",
         hw: HwOpt = None) -> None:
    """Adopt the partition's verified best when it beats your code or your code builds on a refuted run."""
    from .ops import sync as do_sync

    ctx = _ctx(hw)
    r = do_sync(ctx, trust)
    if r["adopted"]:
        text = (f"adopted {r['status']} best {r['code']} (val_bpb={r['value']:.6f}): {r['why']}"
                + ("; committed" if r["committed"] else ""))
    else:
        text = f"no change: {r['reason']}"
    _emit({"ok": True, **r}, text)


@ar_app.command("note")
def note(text: str,
         evidence: Annotated[str, typer.Option("--evidence", help="last (default), an e:id, or none")] = "last",
         hw: HwOpt = None) -> None:
    """Record an observation (supported by your last run by default)."""
    from .ops import note as do_note

    r = do_note(_ctx(hw), text, None if evidence == "none" else evidence)
    _emit({"ok": True, **r}, r["id"] + (" (duplicate statement)" if r["duplicate_of"] else ""))


@ar_app.command("idea")
def idea(text: str, knob: Annotated[list[str] | None, typer.Option("--knob")] = None, hw: HwOpt = None) -> None:
    """Record a hypothesis worth trying (no evidence yet)."""
    from .ops import idea as do_idea

    r = do_idea(_ctx(hw), text, _knobs(knob))
    _emit({"ok": True, **r}, r["id"] + (" (duplicate statement)" if r["duplicate_of"] else ""))


@ar_app.command("status")
def status(all_: Annotated[bool, typer.Option("--all", help="every partition")] = False, hw: HwOpt = None) -> None:
    """Verified best, candidates, in-flight work, replication queue and calibration per partition."""
    from ...core.ledger import Ledger
    from .ops import status as do_status

    part = None
    if not all_:
        try:
            part = _ctx(hw).partition
        except Exception:  # noqa: BLE001 - outside a checkout: show everything
            part = None
    st = _st()
    lg = Ledger.open(project=st["project"], agent=st["agent"] or "human")
    s = do_status(lg, part)
    lines = [f"project {s['project']}"]
    for p in s["partitions"]:
        b = p["verified_best"]
        cal = p["calibration"]
        lines += [
            f"\n{p['partition']}",
            "  verified best  " + (f"{b['mean']:.6f}" + (f" ± {b['std']:.6f}" if b["std"] is not None else "")
                                   + f" (n={b['n']}, code {b['code']}, {b['author']}, {b['claim']})" if b else "none"),
            "  candidates     " + (", ".join(f"{c['value']:.6f} {c['id']} ({c['author']})" for c in p["candidates"])
                                   or "none"),
            "  in flight      " + ("; ".join(f"{f['agent']}: {f['idea'][:50]} ({f['lease_left_s'] // 60}m)"
                                             for f in p["in_flight"]) or "none"),
            "  replication    " + (", ".join(f"{k} {v}" for k, v in sorted(p["replication_tasks"].items()))
                                   or "none") + "  | results " + (", ".join(f"{k} {v}" for k, v in
                                                                            sorted(p["results"].items())) or "none"),
            "  calibration    " + (f"sigma={cal.get('sigma')} epsilon={cal.get('epsilon')} ({cal.get('status')})"
                                   if cal else "pending"),
            f"  runs           {p['runs']} ({p['failed_runs']} failed), duplicates blocked {p['duplicates_blocked']} "
            f"(~{p['gpu_minutes_saved']} GPU min saved)",
            f"  GPU minutes    {p['gpu_minutes']['total']} total, {p['gpu_minutes']['replication']} on replication "
            f"({p['gpu_minutes']['replication_share']:.0%})"]
    if not s["partitions"]:
        lines.append("no runs yet")
    _emit({"ok": True, **s}, "\n".join(lines))


@ar_app.command("import")
def import_cmd(path: Annotated[str | None, typer.Argument(
                   help="results.tsv, or the skill's log (file or the directory that holds it)")] = None,
               source: Annotated[str, typer.Option(
                   "--from", help="whose log: results.tsv, pi-autoresearch, uditgoenka, codex-autoresearch")]
               = "results.tsv",
               metric: Annotated[str | None, typer.Option(
                   "--metric", help="the log's name for the project's metric, when it is not val_bpb")] = None,
               hw: HwOpt = None) -> None:
    """Record an existing results.tsv, or the run log of an autoresearch skill (--from), as first runs:
    train.py of each row comes from git, keeps become candidates and get re-run. --hw (the GPU class the runs
    used) is required. Use --agent to name who ran them. Importing the same log again adds nothing."""
    from pathlib import Path

    from .ops import import_results

    default = "results.tsv" if source == "results.tsv" else "."
    r = import_results(_ctx(hw), Path(path or default), hw, source, metric)
    extra = "".join(f", {r[k]} {label}" for k, label in (("other_metric", "measuring another metric"),
                                                          ("skipped", "not runs")) if r.get(k))
    _emit({"ok": True, **r}, f"imported {r['imported']} of {r['rows']} rows ({r['duplicates']} already recorded, "
                             f"{r['missing_commit']} commits not found{extra})")


@ar_app.command("referee")
def referee_cmd(interval: Annotated[float, typer.Option("--interval", help="seconds between ticks")] = 30,
                once: Annotated[bool, typer.Option("--once", help="one tick and exit")] = False) -> None:
    """Apply the referee rules (they also run after every ar run/replicate/sync)."""
    from ...core.ledger import Ledger
    from .referee import tick

    st = _st()
    lg = Ledger.open(project=st["project"], agent="referee", role="orchestrator")
    while True:
        actions = tick(lg)
        stamp = time.strftime("%H:%M:%S")
        if once:
            _emit({"ok": True, "actions": actions}, "\n".join(f"{stamp} {a}" for a in actions) or "nothing to do")
            return
        for a in actions:
            print(f"{stamp} {a}", flush=True)
        time.sleep(interval)
