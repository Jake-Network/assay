"""The autoresearch side of the runner: the time budget, results.tsv and the val_bpb default (§8 ④⑤⑧).

Running the command itself is core (`benchbook.runner`); its names are re-exported here for the callers that
import them from this module."""
from __future__ import annotations

from pathlib import Path

from ... import runner as core
from ...runner import (
    DEFAULT_PARSE_REGEX,
    Interrupted,
    RunResult,
    _child_dies_with_parent,
    _kill_group,
    _trap_signals,
    parse_summary,
    tail,
)

__all__ = [
    "DEFAULT_PARSE_REGEX",
    "RESULTS_HEADER",
    "TSV_STATUS",
    "Interrupted",
    "RunResult",
    "_child_dies_with_parent",
    "_kill_group",
    "_trap_signals",
    "append_results_tsv",
    "check_budget",
    "parse_summary",
    "results_tsv_line",
    "run_command",
    "tail",
]

RESULTS_HEADER = "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"


def run_command(cmd: str, cwd: Path, log_path: Path, timeout: float, metric: str | None = "val_bpb",
                parse_regex: str = DEFAULT_PARSE_REGEX, env: dict[str, str] | None = None,
                tee: bool = False) -> RunResult:
    """The core runner with this integration's default: a training is ok only with val_bpb in its summary
    (core's default is no metric, exit code 0 alone)."""
    return core.run_command(cmd, cwd, log_path, timeout, metric, parse_regex, env, tee)


def check_budget(res: RunResult, budget_s: float | None, slack_s: float = 30.0,
                 overhead_s: float | None = None) -> RunResult:
    """A successful run that trained longer than the time budget is invalid (A9). training_seconds is what
    train.py reports; the wall clock (budget + overhead_s for startup and evaluation) cannot be faked."""
    if res.run_status != "ok" or not budget_s:
        return res
    problems = []
    ts = res.metrics.get("training_seconds")
    if isinstance(ts, (int, float)) and ts > budget_s + slack_s:
        problems.append(f"training_seconds {ts:g} > {budget_s:g} + {slack_s:g}")
    wall = res.duration_ms / 1000
    if overhead_s is not None and wall > budget_s + overhead_s:
        problems.append(f"wall clock {wall:.0f}s > {budget_s:g} + {overhead_s:g}s for startup and evaluation")
    if not problems:
        return res
    return RunResult("invalid", res.exit_code, res.metrics, res.duration_ms, res.log_tail,
                     "over the time budget: " + "; ".join(problems))


def results_tsv_line(commit: str, val_bpb: float | None, peak_vram_mb: float | None, status: str,
                     description: str) -> str:
    desc = " ".join(description.replace("\t", " ").split())
    if status == "crash" or val_bpb is None:
        return f"{commit}\t0.000000\t0.0\t{status}\t{desc}\n"
    mem = (peak_vram_mb or 0.0) / 1024
    return f"{commit}\t{val_bpb:.6f}\t{mem:.1f}\t{status}\t{desc}\n"


def append_results_tsv(path: Path, line: str) -> None:
    """Upstream format: tab separated, header commit/val_bpb/memory_gb/status/description."""
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write(RESULTS_HEADER)
        f.write(line)


TSV_STATUS = {"improved": "keep", "no_change": "discard", "worse": "discard", "failed": "crash",
              "unscored": "discard"}
