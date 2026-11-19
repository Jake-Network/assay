"""Running the training command, parsing its summary and writing results.tsv (§8 ④⑤⑧)."""
from __future__ import annotations

import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PARSE_REGEX = r"^(\w+):\s+([-\d.eE+]+)\s*$"
RESULTS_HEADER = "commit\tval_bpb\tmemory_gb\tstatus\tdescription\n"
_INT = re.compile(r"^[-+]?\d+$")


@dataclass
class RunResult:
    run_status: str  # ok | error | timeout
    exit_code: int | None
    metrics: dict[str, float | int] = field(default_factory=dict)
    duration_ms: int = 0
    log_tail: str = ""
    reason: str = ""


def parse_summary(text: str, regex: str = DEFAULT_PARSE_REGEX) -> dict[str, float | int]:
    """Every `key: number` line (the last occurrence wins). Non-finite numbers are skipped."""
    out: dict[str, float | int] = {}
    pattern = re.compile(regex, re.MULTILINE)
    for line in text.replace("\r", "\n").splitlines():
        m = pattern.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        try:
            v: float | int = int(raw) if _INT.match(raw) else float(raw)
        except ValueError:
            continue
        if isinstance(v, float) and not math.isfinite(v):
            continue
        out[key] = v
    return out


def tail(text: str, lines: int = 30, chars: int = 1500) -> str:
    return "\n".join(text.replace("\r", "\n").splitlines()[-lines:])[:chars]


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_command(cmd: str, cwd: Path, log_path: Path, timeout: float, metric: str = "val_bpb",
                parse_regex: str = DEFAULT_PARSE_REGEX, env: dict[str, str] | None = None) -> RunResult:
    """Run `cmd` in its own process group with stdout+stderr redirected to log_path. On timeout the
    whole group is killed. ok needs exit code 0 and the metric in the summary."""
    kw: dict[str, object] = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kw["start_new_session"] = True
    t0 = time.monotonic()
    timed_out = False
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,  # noqa: S602
                                stdin=subprocess.DEVNULL, env=env, **kw)  # type: ignore[call-overload]
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
        except BaseException:
            _kill_group(proc)
            raise
    duration_ms = int((time.monotonic() - t0) * 1000)
    text = log_path.read_bytes().decode("utf-8", errors="replace")
    metrics = parse_summary(text, parse_regex)
    if timed_out:
        return RunResult("timeout", None, metrics, duration_ms, tail(text), f"timeout after {timeout:g}s")
    if proc.returncode != 0:
        return RunResult("error", proc.returncode, metrics, duration_ms, tail(text), f"exit {proc.returncode}")
    if metric not in metrics:
        return RunResult("error", 0, metrics, duration_ms, tail(text), f"no {metric} in the summary")
    return RunResult("ok", 0, metrics, duration_ms, tail(text), "")


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
