"""One definition of runs, keeps and bests for both arms of the launch experiment (§13.3, B1-B4).

Both arms are counted from the same uv shim log (wrap_train.sh + shim_log.py); the ledger only adds what the
shim cannot see (B's verdicts, calibration). Runs of the unmodified upstream train.py (the baseline every arm
must run) are reported apart and never counted as duplicates (B4).
"""
from __future__ import annotations

import csv
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from assay.integrations.autoresearch.code import code_hash


def load_runs(out: Path, arm: str) -> list[dict[str, Any]]:
    """Shim runs in start order. finished=False: a start line without an end (killed hard)."""
    f = out / arm / "runs.jsonl"
    if not f.exists():
        return []
    runs: dict[str, dict[str, Any]] = {}
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        r = json.loads(line)
        ev = r.get("event")
        if ev is None:  # one line per finished run (older shim)
            runs[f"line-{i}"] = {**r, "finished": True, "killed": False}
        elif ev == "start":
            runs[r["id"]] = {**r, "finished": False, "killed": False, "exit": None, "end": None, "val_bpb": None}
        elif ev == "end" and r["id"] in runs:
            runs[r["id"]].update({k: v for k, v in r.items() if k not in ("event", "id")}, finished=True)
    return list(runs.values())


def is_ok(r: dict[str, Any]) -> bool:
    return bool(r.get("finished")) and not r.get("killed") and r.get("exit") == 0 and r.get("val_bpb") is not None


def minutes(r: dict[str, Any]) -> float:
    return ((r.get("end") or r["start"]) - r["start"]) / 60


def replication_number(r: dict[str, Any]) -> int:
    return int(r.get("seed") or 0) if r.get("assay_run") else 0


def duplicates(runs: list[dict[str, Any]], baseline_hash: str | None) -> dict[str, Any]:
    """Metric 3: successful runs of code that already ran successfully with the same replication number."""
    seen: set[tuple[str, int]] = set()
    n, mins, base = 0, 0.0, 0
    for r in sorted((r for r in runs if is_ok(r)), key=lambda r: r.get("end") or r["start"]):
        if baseline_hash is not None and r["code_hash"] == baseline_hash:
            base += 1
            continue
        key = (r["code_hash"], replication_number(r))
        if key in seen:
            n += 1
            mins += minutes(r)
        seen.add(key)
    return {"duplicates": n, "minutes": mins, "baseline_runs": base}


def unrecorded(runs: list[dict[str, Any]], ledger_rows: list[dict[str, Any]]) -> dict[str, int]:
    """H1 for arm B: shim runs started by assay ar with no ledger experiment for that code and replication
    number, and runs started outside assay ar (the agent called uv run train.py itself)."""
    started = Counter((r["code_hash"], replication_number(r)) for r in runs if r.get("assay_run"))
    recorded = Counter((e["code_ref"], int(e["seed"] or 0)) for e in ledger_rows)
    return {"not_recorded": sum(max(0, n - recorded[k]) for k, n in started.items()),
            "outside_assay": sum(1 for r in runs if not r.get("assay_run"))}


def code_at(tree: Path, commit: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(tree), "show", f"{commit}:./train.py"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def a_keeps(out: Path, runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Arm A's "keep" rows from every branch's results.tsv. The value is what the shim saw that code print
    (first successful run), falling back to the agent-written results.tsv value."""
    runs = load_runs(out, "A") if runs is None else runs
    shim_value: dict[str, float] = {}
    for r in runs:
        if is_ok(r):
            shim_value.setdefault(r["code_hash"], float(r["val_bpb"]))
    rows = []
    for wt in sorted((out / "A").glob("gpu*")):
        f = wt / "results.tsv"
        if not f.exists():
            continue
        with open(f, encoding="utf-8", newline="") as fh:
            for i, r in enumerate(csv.DictReader(fh, delimiter="\t")):
                if r.get("status", "").strip().lower() != "keep":
                    continue
                src = code_at(wt, r["commit"])
                h = code_hash(src) if src is not None else None
                tsv = float(r["val_bpb"])
                rows.append({"tree": wt, "commit": r["commit"], "code_hash": h, "tsv_value": tsv,
                             "val_bpb": shim_value.get(h or "", tsv), "source": "shim" if h in shim_value else
                             "results.tsv", "description": r.get("description", ""), "baseline": i == 0})
    return rows


def a_best(keeps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """B3: arm A's final best, one definition for post_replicate and report: the lowest shim-observed value
    among keeps."""
    return min(keeps, key=lambda r: r["val_bpb"]) if keeps else None


def b_keeps(lg: Any, baseline_hash: str | None) -> list[dict[str, Any]]:
    """Arm B's KEEP verdicts (first runs better than the verified best and than their parent), the
    counterpart of A's keeps for metric 5. The baseline code is left out as in arm A."""
    rows = lg.sql_internal(
        "SELECT id, code_ref, primary_value, artifacts FROM assay.experiments_valid WHERE project_id=%s"
        " AND run_status='ok' AND coalesce(seed,0)=0 AND outcome='improved'"
        " AND coalesce(trace_ref->>'beats_parent','true') <> 'false' ORDER BY created_at, id", (lg.project_id,))
    out = []
    for r in rows:
        if r["code_ref"] == baseline_hash:
            continue
        uri = next((a["uri"] for a in r["artifacts"] or [] if str(a.get("uri", "")).startswith("assay://blob/")),
                   None)
        out.append({"id": str(r["id"]), "code_hash": r["code_ref"], "val_bpb": float(r["primary_value"]),
                    "uri": uri})
    return out
