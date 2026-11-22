"""Pinned task manifest and independent oracle adapter."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import probes

ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}")
SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Phase:
    prompt: str
    stale_path: str
    stale_piece: str
    fresh_piece: str
    injection: str
    action: str


@dataclass(frozen=True)
class Task:
    id: str
    source: Path
    base: str
    oracle: Path
    phases: tuple[Phase, ...]


def input_tasks(manifest: Path) -> list[Task]:
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("the manifest must be a nonempty JSON array")
    tasks = []
    seen = set()
    for row in rows:
        if not ID.fullmatch(row["id"]) or row["id"] in seen or not SHA.fullmatch(row["base"]):
            raise ValueError("each task needs a unique id and a pinned 40-character base commit")
        phases = tuple(Phase(**phase) for phase in row["phases"])
        if len(phases) < 2 or any(
            not all(isinstance(value, str) and value for value in asdict(phase).values())
            or not ID.fullmatch(phase.stale_piece) or not ID.fullmatch(phase.fresh_piece)
            or phase.stale_piece == phase.fresh_piece for phase in phases
        ):
            raise ValueError("each task needs at least two phases with distinct stale and fresh piece ids")
        source, oracle = ((manifest.parent / row[key]).resolve(strict=True) for key in ("source", "oracle"))
        if not source.is_dir() or not oracle.is_file() or oracle.is_relative_to(source):
            raise ValueError("the source repo and independent oracle must be local and the oracle outside the repo")
        tasks.append(Task(row["id"], source, row["base"], oracle, phases))
        seen.add(row["id"])
    return tasks


def inspect(task: Task, work: Path, phase: int, stage: str, streams: Path,
            before: str | None = None) -> dict[str, Any]:
    call = [sys.executable, "-I", str(task.oracle), "--checkout", str(work), "--base", task.base,
            "--phase", str(phase), "--stage", stage, "--streams", str(streams)]
    if before is not None:
        call.extend(["--before-injection", before])
    receipt = subprocess.run(call, cwd=task.oracle.parent, capture_output=True, check=True, timeout=600, text=True)
    state = json.loads(receipt.stdout)
    if stage != "action":
        expected = state.get("expected")
        if not isinstance(expected, dict) or set(expected) != set(probes.QUESTIONS) or any(
            not isinstance(v, str) or not v for v in expected.values()
        ):
            raise ValueError("oracle must return one exact answer for each state probe")
        if (expected["stale"], expected["fresh"]) != ("no" if stage == "injected" else "yes", "yes"):
            raise ValueError("oracle did not establish the stale and unchanged verification controls")
    names = ("merged", "false_done", "weakened", "repeated", "trusted_stale", "misflagged_fresh")
    if any(type(state.get(key)) is not int or state[key] < 0 for key in names) or any(
        state[key] > state["merged"] for key in ("false_done", "weakened")
    ) or any(
        state[key] > 1 for key in ("trusted_stale", "misflagged_fresh")
    ):
        raise ValueError("oracle returned invalid behavioral counts")
    return state
