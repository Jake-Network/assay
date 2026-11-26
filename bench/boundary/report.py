"""Render the boundary benchmark records as a Markdown report."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import probes as questions
from .arms import ARMS


def rate(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.1%})" if d else f"{n}/{d} (n/a)"


def probe_rate(probes: list[dict[str, Any]], *kinds: str) -> str:
    grades = [correct for probe in probes if probe["boundary"] in kinds for correct in probe["correct"].values()]
    return rate(sum(grades), len(grades))


def contains_rate(probes: list[dict[str, Any]], *kinds: str) -> str:
    """probe_rate under probes.contains, recomputed from the stored expected and answers (older runs too)."""
    grades = [correct for probe in probes if probe["boundary"] in kinds
              for correct in questions.contains(probe["expected"], probe["answers"]).values()]
    return rate(sum(grades), len(grades))


def write(out: Path) -> Path:
    data = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    trials = data["trials"]
    selected = set(data.get("arms", (trial["arm"] for trial in trials)))
    completed: dict[tuple[str, int], set[str]] = defaultdict(set)
    for trial in trials:
        completed[trial["task"], trial["run"]].add(trial["arm"])
    matched = {key for key, finished in completed.items() if selected <= finished}
    lines = ["# Boundary benchmark" + (" (DRY RUN)" if data["dry"] else ""), ""]
    if data["dry"]:
        lines.extend(["> Scripted events and token counts exercise measurement and reporting. "
                      "These results provide no evidence about model or plugin behavior.", ""])
    if "stopped" in data:
        lines.extend(["> Execution stopped. See metrics.json for the stop reason.", ""])
    lines.extend([
        "Comparative totals include only task repetitions completed by every selected arm.", "",
        "| Arm | Matched trials | H1 Compaction answers | H1 Delayed compaction answers | H1 Session answers |"
        " H1 Session answers (contains) | H2 False completions |"
        " H2 Weakened merges | H3 Repeated failures | H4 Cost USD | H4 Session tokens | H4 Wall seconds |"
        " H4 Peak lead tokens | H4 Probe cost USD | H6 Trusted stale | H6 Misflagged fresh |",
        "|" + "---|" * 16,
    ])
    if not matched:
        lines.extend(["", "No matched trials completed."])
    for arm, config in ARMS.items():
        rows = [r for r in trials if r["arm"] == arm and (r["task"], r["run"]) in matched]
        if not rows:
            continue
        phases = [p for r in rows for p in r["phases"]]
        actions = [p["action"] for p in phases]
        calls = [c for r in rows for c in r["calls"]]
        probes = [p for r in rows for p in r["probes"]]
        compact = probe_rate(probes, "compaction")
        delayed = probe_rate(probes, "delayed compaction")
        restart = probe_rate(probes, "fresh session", "resumed session")
        restart_contains = contains_rate(probes, "fresh session", "resumed session")
        merged = sum(p["merged"] for p in phases)
        false_done = sum(p["false_done"] for p in phases)
        weakened = sum(p["weakened"] for p in phases)
        repeated = sum(p["repeated"] for p in [*phases, *actions])
        cost = sum(c["cost_usd"] for c in calls)
        tokens = sum(c["tokens"] for c in calls)
        seconds = sum(r["wall_seconds"] for r in rows)
        probe_cost = sum(c["cost_usd"] for c in calls if c["kind"] == "probe")
        peak = max(c["lead_context_max"] for c in calls)
        trusted_stale = sum(p["trusted_stale"] for p in actions)
        misflagged_fresh = sum(p["misflagged_fresh"] for p in actions)
        lines.append(f"| {arm} {config.label} | {len(rows)} | {compact} |"
                     f" {delayed} | {restart} | {restart_contains} | {rate(false_done, merged)} | {weakened}/{merged} |"
                     f" {repeated} | {cost:.4f} | {tokens} | {seconds:.2f} | {peak} | {probe_cost:.4f} |"
                     f" {rate(trusted_stale, len(actions))} | {rate(misflagged_fresh, len(actions))} |")
    lines.extend([
        "", "H1 scores the five fixed questions against the task oracle. Arm A resumes the original session;"
        " the other arms start a new one at each session boundary. The contains column is a sensitivity column"
        " (an identifier answer counts when it contains the expected identifier); the registered measure is the"
        " exact match.",
        "Compaction samples require a Claude Code `compact_boundary` event. The first invocation after the work"
        " call is the resume probe. A lead response between those events puts the sample in the delayed column.",
        "H4 totals include probe calls. Wall seconds cover completed trials end to end. Claude-mem worker spending"
        " requires a separate receipt from its provider before comparing total cost.",
        "H6 counts subsequent actions after changing one previously verified file, with an unchanged piece as a"
        " control. Per-probe scores also show each piece's applicability answer.", "",
    ])
    if unmatched := completed.keys() - matched:
        lines.extend([
            "## Unmatched completed trials", "",
            "Individual trial measurements are available in metrics.json.", "",
            "| Task | Repetition | Completed arms | Missing arms |", "|---|---|---|---|",
        ])
        for task, run in sorted(unmatched):
            finished = completed[task, run]
            missing = selected - finished
            lines.append(f"| {task} | {run} | {', '.join(sorted(finished))} | {', '.join(sorted(missing))} |")
        lines.append("")
    lines.extend([
        "## H5 fan-in", "",
        "| Circle-packing configuration | Requested workers | Delivered reports | Lead report tokens (estimate) |"
        " Peak lead context (tokens) |", "|---|---|---|---|---|",
    ])
    for fan in data["fan_in"]:
        lines.append(f"| {fan['config']} | {fan['workers']} | {fan['delivered_worker_reports']} |"
                     f" {fan['lead_report_tokens']} | {fan['lead_context_max']} |")
    if not data["fan_in"]:
        lines.append("| Run independently with `python -m bench.run --workers 8,16,30` | n/a | n/a | n/a | n/a |")
    path = out / "report.md"
    path.write_text("\n".join([*lines, ""]), encoding="utf-8")
    return path


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("out", type=Path)
    print(write(cli.parse_args().out))


if __name__ == "__main__":
    main()
