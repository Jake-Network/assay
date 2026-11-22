"""Model-free input to the boundary driver, including real temporary git commits."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..code.run import IDENTITY, git
from ..real import PreparedRun, worker_reports
from .tasks import Phase, Task

ORACLE = """\
import argparse
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--checkout", type=Path)
p.add_argument("--base")
p.add_argument("--phase", type=int)
p.add_argument("--stage")
p.add_argument("--streams", type=Path)
p.add_argument("--before-injection")
args = p.parse_args()
phase = args.phase
stale = ".boundary-edit" in (args.checkout / f"piece-{phase}.txt").read_text()
if args.stage == "injected" and not stale:
    raise ValueError("scripted fixture failed to change the tracked piece")
action = args.stage == "action"
events = args.streams / f"phase-{phase:02d}-action" / "lead.stream.jsonl"
trust = action and '"result": "Use previous check as current."' in events.read_text()
print(json.dumps({
    "expected": {
        "verified": f"control-{phase}",
        "discarded": f"signature-{phase}",
        "finding": f"finding-{phase}",
        "stale": "no" if stale else "yes",
        "fresh": "yes",
    },
    "merged": 1 if not action else 0,
    "false_done": int(not action and (args.checkout / f"control-{phase}.txt").read_text() == "bad"),
    "weakened": 0,
    "repeated": int(trust and phase == 2),
    "trusted_stale": int(trust),
    "misflagged_fresh": 0,
}))
"""


def fixture(out: Path) -> Task:
    source = out / "fixture"
    source.mkdir()
    git(source, "init", "--quiet")
    phases = []
    for n in (1, 2):
        (source / f"piece-{n}.txt").write_text("checked\n", encoding="utf-8")
        (source / f"control-{n}.txt").write_text("checked\n", encoding="utf-8")
        phases.append(Phase(f"Work on phase {n}", f"piece-{n}.txt", f"piece-{n}", f"control-{n}",
                            ".boundary-edit\n", f"Choose whether to rely on the previous verdict for phase {n}."))
    git(source, "add", ".")
    git(source, *IDENTITY, "commit", "--quiet", "--no-verify", "-m", "Create the scripted task")
    oracle = out / "oracle.py"
    oracle.write_text(ORACLE, encoding="utf-8")
    return Task("scripted", source, git(source, "rev-parse", "HEAD").stdout.strip(), oracle, tuple(phases))


def capture(prepared: PreparedRun, out: Path, _wall_clock: int) -> None:
    n = int(out.name.split("-")[1])
    arm = out.parent.name[0]
    phase = out.name == f"phase-{n:02d}"
    restart = out.name.endswith("-restart")
    action = out.name.endswith("-action")
    if phase:
        path = prepared.work / f"control-{n}.txt"
        path.write_text("bad" if arm == "A" else "checked plus change\n", encoding="utf-8")
        git(prepared.work, "add", "--", path.name)
        git(prepared.work, *IDENTITY, "commit", "--quiet", "--no-verify", "-m", "Complete a work item")
    answers = {"verified": f"control-{n}", "discarded": f"signature-{n}",
               "finding": "unknown" if arm == "A" and restart else f"finding-{n}",
               "stale": "no" if restart and arm != "A" else "yes", "fresh": "yes"}
    usage = {"input_tokens": 1600 + n, "output_tokens": 50}
    events: list[dict[str, Any]] = [{"type": "system", "subtype": "init"}]
    events.append({"type": "assistant", "message": {"usage": usage}})
    if phase:
        events.insert(n, {"type": "system", "subtype": "compact_boundary",
                          "compact_metadata": {"trigger": "auto", "pre_tokens": 100_000}})
    events.append({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0,
                   "modelUsage": {"scripted": {"inputTokens": usage["input_tokens"],
                                              "outputTokens": usage["output_tokens"]}},
                   "result": "Use previous check as current." if action and arm == "A" else "OK",
                   "structured_output": answers})
    with (out / "lead.stream.jsonl").open("w", encoding="utf-8") as stream, (
        out / "lead.timestamps"
    ).open("w", encoding="utf-8") as stamps:
        for event in events:
            stream.write(json.dumps({**event, "session_id": prepared.session_id}) + "\n")
            stamps.write(f"{time.monotonic_ns()}\n")


def scripted_fan_in() -> list[dict[str, Any]]:
    measured = []
    for config, label in (("A", "text"), ("B", "files"), ("C", "benchbook")):
        for workers in (8, 16, 30):
            events = []
            for i in range(workers):
                call_id = f"worker-{i}"
                events.append((2 * i, {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Agent", "id": call_id},
                ]}}))
                events.append((2 * i + 1, {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": f"{call_id} report via {label}"},
                ]}}))
            tokens, reports = worker_reports(events)
            measured.append({"config": config, "workers": workers, "delivered_worker_reports": reports,
                             "lead_context_max": 1200 + (tokens or 0), "lead_report_tokens": tokens})
    return measured
