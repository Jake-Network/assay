"""Run log for the launch experiment's uv shim (wrap_train.sh), the same in both arms (B1).

    python shim_log.py start            -> appends a start line to $AR_BENCH_LOG, prints the run id
    python shim_log.py end ID EXIT LOG  -> appends the matching end line (EXIT "killed" when the shim was signalled)

A start line without an end line is a run that was killed hard (counted, never dropped). The code hash is
assay's code_hash when assay is importable (same identity as the ledger), else sha256 of the raw bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid


def _hash(src: bytes) -> str:
    try:
        from assay.integrations.autoresearch.code import code_hash
    except ImportError:
        return "raw-" + hashlib.sha256(src).hexdigest()
    return code_hash(src)


def _append(rec: dict[str, object]) -> None:
    fd = os.open(os.environ["AR_BENCH_LOG"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(rec) + "\n").encode())
    finally:
        os.close(fd)


def start() -> None:
    rid = uuid.uuid4().hex
    try:
        src = open("train.py", "rb").read()
    except OSError:
        src = b""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    seed = os.environ.get("ASSAY_AR_SEED")
    _append({"event": "start", "id": rid, "agent": os.environ.get("AR_BENCH_AGENT"), "start": time.time(),
             "code_hash": _hash(src), "commit": commit, "cwd": os.getcwd(),
             "assay_run": os.environ.get("ASSAY_AR_RUN"), "seed": int(seed) if seed and seed.isdigit() else None})
    print(rid)


def end(rid: str, code: str, log: str) -> None:
    try:
        text = open(log, encoding="utf-8", errors="replace").read()
    except OSError:
        text = ""
    m = {k: float(v) for k, v in re.findall(r"^(\w+):\s+([-\d.eE+]+)\s*$", text, re.M)}
    killed = code == "killed"
    _append({"event": "end", "id": rid, "end": time.time(), "exit": None if killed else int(code), "killed": killed,
             "val_bpb": m.get("val_bpb"), "peak_vram_mb": m.get("peak_vram_mb"),
             "training_seconds": m.get("training_seconds")})


if __name__ == "__main__":
    if sys.argv[1] == "start":
        start()
    else:
        end(sys.argv[2], sys.argv[3], sys.argv[4])
