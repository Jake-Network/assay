"""`benchbook check`: the checks declared in .benchbook/config.toml [checks] (docs/code.md).
Exit codes: 0 every check passed, 1 a check failed or a user error, 128+N interrupted by signal N."""
from __future__ import annotations

import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from .check_run import run as run_command

if TYPE_CHECKING:
    from ..checks.verify import Recorded, Verification

check_app = typer.Typer(no_args_is_help=True, help="Declared checks: re-run them on a reported commit.")
check_app.command("run")(run_command)


@check_app.command("verify")
def verify(
    piece: Annotated[str, typer.Argument(help="The piece (unit of work) the commit completes")],
    commit: Annotated[str, typer.Argument(help="The commit reported as done (any commit-ish)")],
    reported_by: Annotated[str | None, typer.Option(
        "--reported-by", help="The agent that reported the piece done; its completion claim is recorded in its name")]
    = None,
    report_file: Annotated[Path | None, typer.Option(
        "--report-file", help="Its report; only a hash, the size and the check names it mentions are kept")] = None,
    base: Annotated[str | None, typer.Option(
        "--base", help="The ref the piece branched from (default: base in config.toml's guard table, else "
                       "origin/main)")] = None,
    checks: Annotated[str | None, typer.Option(
        "--checks", help="Run only these declared checks (comma separated). The run is recorded as partial and never "
                         "verifies the completion")] = None,
    claim: Annotated[str | None, typer.Option(
        "--claim", help="The completion claim this re-run is evidence for (its id, as pr pieces --claims prints it). "
                        "It must be about PIECE at COMMIT; its reporter is the implementer")] = None,
    trace_ref: Annotated[str | None, typer.Option(
        "--trace-ref", help="JSON object (or @file) stored as the attempt's trace_ref, e.g. the CI run's URL")] = None,
    no_decide: Annotated[bool, typer.Option(
        "--no-decide", help="Record only: decide nothing, even with lead authority (what CI does by default)")]
    = False,
) -> None:
    """Re-run the declared checks on COMMIT in a clean worktree and record them against PIECE's completion.

    The checks are those declared at the merge-base of the base and COMMIT, never the ones COMMIT brings.

    The verifier is --agent or BENCHBOOK_AGENT (or BENCHBOOK_TOKEN's agent), else verifier@<host>; it must differ
    from --reported-by. With lead authority (orchestrator or human role) verify also decides: verified, inconclusive,
    or nothing when a guarded path or [checks] changed (docs/code.md)."""
    from ..checks.verify import default_verifier
    from ..checks.verify import verify as run_verify
    from ..config import env
    from ..core.ledger import Ledger
    from ..core.runner import Interrupted
    from .main import STATE, csv, die, emit, load_json

    trace = load_json(trace_ref, "--trace-ref")
    if trace is not None and not isinstance(trace, dict):
        die("--trace-ref: must be a JSON object")
    name = STATE["agent"] or env("AGENT")
    token = env("TOKEN")  # the token names the verifier; a default name would contradict it
    lg = Ledger.open(project=STATE["project"], agent=name or (None if token else default_verifier()),
                     role=None if name or token else "harness")
    want = lg.pack.project_defaults
    if want and (lg.project.primary_metric != want.get("metric")
                 or lg.project.settings.get("partition_key") != want.get("settings", {}).get("partition_key")):
        print(f"warning: {lg.project_id} was not created with the {lg.pack.name} pack's defaults (benchbook init "
              f"--pack {lg.pack.name}); the false completion query in docs/code.md will not see its guard columns",
              file=sys.stderr)
    try:
        v, rec = run_verify(lg, piece, commit, repo=Path.cwd(), base=base, only=csv(checks) or None,
                            reported_by=reported_by, report_file=report_file, trace_ref=trace,
                            decide=not no_decide, claim=claim)
    except (Interrupted, KeyboardInterrupt) as e:
        signum = e.signum if isinstance(e, Interrupted) else int(signal.SIGINT)
        print(f"interrupted ({signal.Signals(signum).name}): the worktree was removed and the attempt recorded "
              "with run_status error", file=sys.stderr)
        for note in getattr(e, "__notes__", []):
            print(note, file=sys.stderr)
        raise typer.Exit(128 + signum) from None
    if rec.notice is not None and not STATE["json"]:
        print(f"notice: {rec.notice}", file=sys.stderr)
    emit(summary(v, rec), render(v, rec))
    if not v.all_passed:
        raise typer.Exit(1)


@check_app.command("pin")
def pin(
    piece: Annotated[str | None, typer.Argument(help="The piece whose checks to pin (default: the project's)")] = None,
    check: Annotated[list[str] | None, typer.Option(
        "--check", help="NAME=CMD to pin instead of config.toml's [checks] (repeatable); {junit} in CMD reads a JUnit "
                        "report")] = None,
) -> None:
    """Pin the checks check verify runs for PIECE (or every piece) in the ledger, so nothing has to be committed.

    Without --check the [checks], [guard] and [verify] tables of the working tree's config.toml are pinned. check
    verify reads a pin only when the merge-base has no committed config.toml, ignores pins written by the piece's
    implementer, and sends the piece to a person when the pin changed after its first attempt (docs/code.md)."""
    from ..checks.declarations import parse_check_options, pin_checks
    from ..config import load
    from ..core.ids import short
    from .main import emit, ledger

    cfg = load()
    specs = parse_check_options(check) if check else cfg.checks
    p, new = pin_checks(ledger(), piece, specs, cfg.guard_paths, cfg.verify.table(),
                         source="cli" if check else "config")
    scope = "the project" if piece is None else f"piece {piece}"
    emit({"ok": True, "pin": short("claim", p.id), "piece": p.piece, "new": new, "sha256": p.sha256,
          "checks": list(p.checks)},
         f"{'pinned' if new else 'already pinned'} {', '.join(p.checks)} for {scope} ({short('claim', p.id)}, "
         f"{p.sha256[:12]})")


def summary(v: Verification, rec: Recorded) -> dict[str, Any]:
    from ..core.ids import short

    out = rec.outcome
    return {
        "ok": v.all_passed, "piece": v.piece, "commit": v.commit, "base": v.base.ref, "merge_base": v.base.merge_base,
        "results": [{"name": r.name, "status": r.status, "exit_code": r.exit_code, "duration_ms": r.duration_ms,
                    "failed_tests": [t for t, _ in r.failed_tests]} for r in v.results],
        **v.metrics(),
        "experiment": short("experiment", rec.experiment_id), "verifier": rec.verifier,
        "claim": short("claim", rec.claim_id) if rec.claim_id else None, "reported_by": rec.reported_by,
        "relation": rec.relation,
        "duplicate_of": short("experiment", rec.duplicate_of) if rec.duplicate_of else None, "notice": rec.notice,
        "decision": out.decision if out else None, "decision_reason": out.reason if out else None,
        "needs_person": out.person if out else False,
        "evidence": [short("experiment", e) for e in out.evidence] if out else [],
        "decisions": [short("decision", d) for d in out.decision_ids] if out else [],
        "seen_before": [short("experiment", p.id) for p in rec.seen_before],
        "seen_before_approaches": list(dict.fromkeys(p.approach for p in rec.seen_before if p.approach)),
        "discarded": short("claim", rec.discarded_id) if rec.discarded_id else None,
    }


def render(v: Verification, rec: Recorded) -> str:
    from ..checks.failures import notice
    from ..core.ids import short

    width = max(len("check"), *(len(r.name) for r in v.results))
    lines = [f"verify {v.piece} at {v.commit[:12]} (base {v.base.ref}, merge-base {v.base.merge_base[:12]})",
             f"  {'check':<{width}}  {'result':<7}  {'time':>7}"]
    for r in v.results:
        row = f"  {r.name:<{width}}  {r.status:<7}  {f'{r.duration_ms / 1000:.1f}s':>7}"
        if r.failed_tests:
            shown = ", ".join(f"{t} ({k})" if k else t for t, k in r.failed_tests[:5])
            more = f" and {len(r.failed_tests) - 5} more" if len(r.failed_tests) > 5 else ""
            row += f"  failing: {shown}{more}"
        elif r.status == "fail":
            row += f"  exit {r.exit_code}"
        lines.append(row)
        if r.status in ("fail", "timeout") and not r.failed_tests and r.tail:
            lines += [f"      | {x}" for x in r.tail.splitlines()[-8:]]
    lines.append(f"checks {v.passed}/{v.total} passed")
    if v.environment:
        lines.append(f"environment: {v.environment}; not counted against the piece")
    if v.missing:
        lines.append(f"partial: {v.total} of {len(v.declared)} declared checks run (not run: {', '.join(v.missing)}); "
                     "recorded, but only a run of every declared check can verify")
    integ = v.integrity
    if integ.guard_weakened_files:
        files = integ.guard_weakened_files
        lines.append(f"guard: {len(files)} test file(s) weakened since the merge-base (lines deleted or changed, a "
                     "skip marker, or conftest): " + ", ".join(files[:10]) + (" ..." if len(files) > 10 else ""))
    if integ.fewer_tests:
        lines.append(f"guard: fewer tests ran than at the merge-base in {', '.join(integ.fewer_tests_in)} "
                     f"({integ.tests_after} in all, {integ.tests_before} at the merge-base)")
    if added := integ.guard_added + integ.guard_appended:
        lines.append(f"tests added (not a weakening): {', '.join(added[:10])}" + (" ..." if len(added) > 10 else ""))
    if v.integrity.checks_changed:
        lines.append("checks_changed: the commit's [checks] differ from the merge-base's (the merge-base's ran)")
    rel = ""
    if rec.claim_id is not None:
        rel = (f" {rec.relation or 'recorded beside'} {short('claim', rec.claim_id)} "
               f"(completion reported by {rec.reported_by})")
    lines.append(f"recorded {short('experiment', rec.experiment_id)} as {rec.verifier}{rel}")
    if seen := notice(rec.seen_before, v.piece):
        lines.append(seen)
    if rec.discarded_id is not None:
        lines.append(f"discarded approach: {short('claim', rec.discarded_id)}"
                     + ("" if rec.discarded_new else " (this failure was recorded there before; the run is added)"))
    elif rec.discarded_error:
        lines.append(f"discarded approach not recorded: {rec.discarded_error}")
    if rec.outcome is not None:
        lines.append(f"decision: {rec.outcome.reason}")
        if rec.outcome.guard_files:
            lines += [f"  changed: {f}" for f in rec.outcome.guard_files[:20]]
            if len(rec.outcome.guard_files) > 20:
                lines.append(f"  ... and {len(rec.outcome.guard_files) - 20} more")
        if rec.outcome.checks_changed:
            lines.append("  changed: the [checks] table of .benchbook/config.toml")
    return "\n".join(lines)
