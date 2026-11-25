"""`benchbook ar gate`: the reproduction gate in one command (PR 2-R1).

The gate is nothing new. It is what `benchbook ar` already does for a project that `ar init` created: a first
run that beats the verified best is only a candidate, the referee has other agents re-run it, and the ledger
lets a result become verified only with enough runs from enough authors. All of that hangs on five project
settings. `gate on` puts them at their defaults wherever they are missing or weaker, creating the project first
when the checkout has none, so a checkout driven by an autoresearch skill gets the gate with one line.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from . import skills

# The ledger and the operations are imported inside the functions: this module is loaded on every CLI start
# (hooks included), which has to stay fast.
gate_app = typer.Typer(no_args_is_help=True, help="The reproduction gate: only replicated results count.")


@gate_app.callback()
def _group() -> None:
    """A callback keeps `gate` a group while it has one command (typer would otherwise drop the `on`)."""


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def gate_patch(settings: dict[str, Any]) -> dict[str, Any]:
    """The settings changes that turn the gate on. Values that are already as strict or stricter stay."""
    from .ops import PROJECT_SETTINGS as d  # noqa: N811

    patch: dict[str, Any] = {}
    if settings.get("outcome_baseline") != "verified":
        patch["outcome_baseline"] = "verified"  # "improved" means better than the replicated best
    if not settings.get("partition_key"):
        patch["partition_key"] = d["partition_key"]  # candidates exist per partition
    reps = _int(settings.get("replicates"))
    if reps is None or reps < 1:
        reps = patch["replicates"] = d["replicates"]  # re-runs per candidate
    authors = _int(settings.get("rerun_min_authors"))
    if authors is None or authors < 2:
        patch["rerun_min_authors"] = d["rerun_min_authors"]  # someone other than the original author
    seeds = _int(settings.get("rerun_min_seeds"))
    if seeds is None or seeds < 2:
        # the first run plus its re-runs; never more than the referee schedules, or nothing could be verified
        patch["rerun_min_seeds"] = min(d["rerun_min_seeds"], reps + 1)
    return patch


def gate_on(project: str, hw: str | None = None, start: Path | None = None, skill: str | None = None,
            force: bool = False) -> dict[str, Any]:
    from ...core.ledger import Ledger
    from ...errors import BenchbookError, NotFound
    from . import hw as hwmod
    from . import ops, referee
    from .checkout import Checkout

    if skill is not None and skill not in skills.SKILLS:
        raise BenchbookError(f"--from must be one of: {', '.join(skills.SKILLS)}")
    co = Checkout.find(start)
    created = False
    try:
        lg = Ledger.open(project=project, agent="human")
    except NotFound:
        # ar init already creates a gated project; a skill's checkout keeps the skill's loop, so no program file
        ops.init(project, hw=hw, start=co.root, program=skill is None)
        lg = Ledger.open(project=project, agent="human")
        created = True
    if lg.project.pack != "ml-research":
        raise BenchbookError(f"project {lg.project_id} uses pack {lg.project.pack!r}; the reproduction gate needs an "
                             "ml-research project (choose another --project)")
    referee.referee_ledger(lg)  # the agent that schedules re-runs and judges them
    patch = gate_patch(lg.refresh().settings)
    if patch:
        lg.update_settings(patch)
    settings = lg.refresh().settings
    hw_class = hwmod.detect(hw)
    budget, slack, _ = ops.budget_of(settings, (co.root / "prepare.py").read_bytes())
    out: dict[str, Any] = {
        "project": lg.project_id, "created": created, "changed": patch, "hw": hw_class,
        "partition": co.partition(hw_class),
        "settings": {k: settings.get(k) for k in ("outcome_baseline", "replicates", "rerun_min_seeds",
                                                  "rerun_min_authors", "partition_key")},
        "time_budget_s": budget, "time_budget_slack_s": slack, "skill": skill, "fragment": None,
        "wrote_fragment": False, "fragment_differs": False, "attach": None, "import": None}
    if skill is not None:
        text = skills.fragment(skill, hw_class)
        dest = co.root / f"program.benchbook.{skill}.md"
        if force or not dest.exists():
            dest.write_text(text, encoding="utf-8")
            out["wrote_fragment"] = True
        else:
            out["fragment_differs"] = dest.read_text(encoding="utf-8") != text
        info = skills.SKILLS[skill]
        out.update(fragment=str(dest), attach=info.attach,
                   **{"import": f"benchbook ar import --from {skill} {info.log_hint} --hw {hw_class}"})
    # candidates recorded while the gate was weaker get their re-runs scheduled now
    out["referee"] = ops.safe_tick(lg)
    return out


@gate_app.command("on")
def on(skill: Annotated[str | None, typer.Option(
           "--from", help="also write the program fragment for a skill: " + ", ".join(skills.SKILLS))] = None,
       project: Annotated[str | None, typer.Option("--project", help="project id (default: autoresearch)")] = None,
       hw: Annotated[str | None, typer.Option("--hw", help="hardware class override")] = None,
       force: Annotated[bool, typer.Option("--force", help="overwrite the program fragment")] = False) -> None:
    """Turn the reproduction gate on for this checkout's project (creating it if needed). Safe to repeat."""
    from ...cli import STATE, emit
    from ...config import load

    pid = project or STATE["project"] or load().project or "autoresearch"
    r = gate_on(pid, hw=hw, skill=skill, force=force)
    s = r["settings"]
    lines = [f"reproduction gate on: project {r['project']}" + (" (created)" if r["created"] else "")
             + f", partition {r['partition']}",
             f"  a run is 'improved' only against the {s['outcome_baseline']} best  (outcome_baseline)",
             f"  each candidate is re-run {s['replicates']} time(s) by other agents  (replicates)",
             f"  verified needs {s['rerun_min_seeds']} runs from {s['rerun_min_authors']} authors  "
             "(rerun_min_seeds, rerun_min_authors)",
             "  time budget check: " + (f"on, {r['time_budget_s']:g}s + {r['time_budget_slack_s']:g}s slack"
                                        if r["time_budget_s"] else
                                        "OFF (time_budget_s is 0): over-budget runs are not caught"),
             "  changed: " + (", ".join(f"{k}={v}" for k, v in r["changed"].items()) or
                              "nothing, the gate was already on")]
    lines += [f"  referee: {a}" for a in r["referee"]]
    if r["fragment"]:
        state = "written" if r["wrote_fragment"] else (
            "kept (differs from this benchbook version; --force to overwrite)" if r["fragment_differs"] else "kept")
        lines += [f"  program fragment: {r['fragment']} ({state}); add it to {r['attach']}",
                  f"Record the skill's runs:  {r['import']}"]
    lines.append("Other agents re-run candidates with:  benchbook ar replicate --auto")
    emit({"ok": True, **r}, "\n".join(lines))
