"""One definition of runs, keeps and bests for both arms of the launch experiment (§13.3, B1-B4).

Both arms are counted from the same uv shim log (wrap_train.sh + shim_log.py); the ledger only adds what the
shim cannot see (B's verdicts, calibration). Runs of the unmodified upstream train.py (the baseline every arm
must run) are reported apart and never counted as duplicates (B4).
"""
from __future__ import annotations

import csv
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from benchbook.integrations.autoresearch.code import code_hash


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
            if r["id"] in runs:
                raise ValueError(f"duplicate benchmark start receipt {r['id']} in {f}")
            runs[r["id"]] = {**r, "finished": False, "killed": False, "exit": None, "end": None, "val_bpb": None}
        elif ev == "end" and r["id"] in runs and not runs[r["id"]]["finished"]:
            runs[r["id"]].update({k: v for k, v in r.items() if k not in ("event", "id")}, finished=True)
        else:
            raise ValueError(f"unexpected benchmark receipt at {f}:{i + 1}")
    return list(runs.values())


def is_ok(r: dict[str, Any]) -> bool:
    return bool(r.get("finished")) and not r.get("killed") and r.get("exit") == 0 and r.get("val_bpb") is not None


def minutes(r: dict[str, Any]) -> float:
    return ((r.get("end") or r["start"]) - r["start"]) / 60


def replication_number(r: dict[str, Any]) -> int:
    return int(r.get("seed") or 0) if r.get("benchbook_run") else 0


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
    """Reconcile the ledger and the independent collector in both directions for arm B."""
    started = Counter((r.get("benchbook_run_id"), r.get("agent"), r["code_hash"], replication_number(r))
                      for r in runs if r.get("benchbook_run"))
    recorded = Counter((e.get("ar_run_id"), e.get("author"), e["code_ref"], int(e["seed"] or 0)) for e in ledger_rows)
    return {"not_recorded": (started - recorded).total(), "ledger_only": (recorded - started).total(),
            "outside_benchbook": sum(1 for r in runs if not r.get("benchbook_run"))}


def code_at(tree: Path, commit: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(tree), "show", f"{commit}:./train.py"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def a_keeps(out: Path, runs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Attribute each keep to one successful training receipt from that worker's checkout."""
    runs = load_runs(out, "A") if runs is None else runs
    by_code: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        if is_ok(r):
            by_code[(r.get("agent"), r["code_hash"])].append(r)
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
                candidates = by_code[(wt.name, h)] if h is not None else []
                matches = [j for j, run in enumerate(candidates) if abs(float(run["val_bpb"]) - tsv) <= 0.00000051]
                exact_commit = [j for j in matches if candidates[j].get("commit") == r["commit"]]
                chosen = exact_commit or matches
                run = candidates.pop(chosen[0]) if chosen else None
                rows.append({"tree": wt, "commit": r["commit"], "code_hash": h, "tsv_value": tsv,
                             "val_bpb": run["val_bpb"] if run else None, "source": "shim" if run else "unmatched",
                             "end": run["end"] if run else None, "description": r.get("description", ""),
                             "baseline": i == 0})
    return rows


def a_best(keeps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """B3: arm A's final best, one definition for post_replicate and report: the lowest shim-observed value
    among keeps."""
    return min((k for k in keeps if k["val_bpb"] is not None), key=lambda r: r["val_bpb"], default=None)


def b_keeps(lg: Any, baseline_hash: str | None) -> list[dict[str, Any]]:
    """Arm B's local KEEP verdicts, the counterpart of A's keeps for metric 5."""
    rows = lg.sql_internal(
        "SELECT id, code_ref, primary_value, artifacts FROM benchbook.experiments_valid WHERE project_id=%s"
        " AND run_status='ok' AND coalesce(seed,0)=0 AND"
        " COALESCE(trace_ref->>'kept', (outcome='improved' AND"
        " coalesce(trace_ref->>'beats_parent','true') <> 'false')::text)='true' ORDER BY created_at, id",
        (lg.project_id,))
    out = []
    for r in rows:
        if r["code_ref"] == baseline_hash:
            continue
        uri = next((a["uri"] for a in r["artifacts"] or [] if str(a.get("uri", "")).startswith("benchbook://blob/")),
                   None)
        out.append({"id": str(r["id"]), "code_hash": r["code_ref"], "val_bpb": float(r["primary_value"]),
                    "uri": uri})
    return out
