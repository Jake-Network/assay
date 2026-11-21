"""Run log for the launch experiment's uv shim (wrap_train.sh), the same in both arms (B1).

    python shim_log.py start            -> appends a start line to $AR_BENCH_LOG, prints the run id
    python shim_log.py end ID EXIT LOG  -> appends the matching end line (EXIT "killed" when the shim was signalled)

A start line without an end line retains the attempt. Training is identified by the invoked script path in
the supported uv and Python command forms. Arm B's run receipt joins the independent log to the ledger.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from benchbook.config import env
from benchbook.integrations.autoresearch.code import code_hash
from benchbook.integrations.autoresearch.runner import parse_summary

UV_PATH_OPTIONS = {"--directory", "--project"}
UV_VALUE_OPTIONS = {"--python", "-p", "--with", "--with-editable", "--with-requirements", "--index", "--index-url",
                    "-i", "--extra-index-url", "--default-index", "--find-links", "-f", "--index-strategy",
                    "--keyring-provider", "--resolution", "--prerelease", "--fork-strategy", "--exclude-newer",
                    "--exclude-newer-package", "--link-mode", "--env-file", "--cache-dir", "--config-file",
                    "--group", "--only-group", "--no-group", "--extra", "--no-extra", "--package",
                    "--no-sources-package", "--upgrade-package", "-P", "--reinstall-package", "--refresh-package",
                    "--python-platform",
                    "--allow-insecure-host", "-X", *UV_PATH_OPTIONS}


def _skip_flags(args: list[str], value_options: set[str]) -> tuple[list[str], Path]:
    cwd = Path.cwd()
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        flag, sep, value = args[i].partition("=")
        i += 1
        if flag == "--":
            break
        if flag in value_options:
            if not sep:
                if i == len(args):
                    return [], cwd
                value, i = args[i], i + 1
            # --project locates metadata but uv runs the script from its current directory.
            if flag == "--directory":
                cwd = (cwd / value).resolve()
    return args[i:], cwd


def _python_name(name: str) -> bool:
    return re.fullmatch(r"python(?:[23](?:\.\d+)?)?(?:\.exe)?", Path(name).name) is not None


def training_path(command: str, args: list[str]) -> Path | None:
    """Resolve the script for `uv run [options] [python] train.py` and direct PATH Python calls."""
    cwd = Path.cwd()
    if command == "uv":
        args, global_cwd = _skip_flags(args, UV_VALUE_OPTIONS)
        if not args or args[0] != "run":
            return None
        args, cwd = _skip_flags(args[1:], UV_VALUE_OPTIONS)
        if cwd == Path.cwd():
            cwd = global_cwd
    if command != "uv" or (args and _python_name(args[0])):
        args = args if command != "uv" else args[1:]
        # -m and -c execute modules and arbitrary code respectively, not a script argument.
        for opt in args:
            if not opt.startswith("-") or opt == "--":
                break
            if opt.startswith(("-m", "-c")):
                return None
        args, _ = _skip_flags(args, {"-W", "-X", "--check-hash-based-pycs"})
    if not args or Path(args[0]).name != "train.py":
        return None
    return (cwd / args[0]).resolve()


def _append(rec: dict[str, object]) -> None:
    fd = os.open(os.environ["AR_BENCH_LOG"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        line = (json.dumps(rec) + "\n").encode()
        if os.write(fd, line) != len(line):
            raise OSError("short write to benchmark training log")
    finally:
        os.close(fd)


def start(path: Path) -> None:
    rid = uuid.uuid4().hex
    src = path.read_bytes()
    try:
        commit = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--short=7", "HEAD"],
                                capture_output=True, text=True,
                                timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    seed = env("AR_SEED")
    _append({"event": "start", "id": rid, "agent": os.environ.get("AR_BENCH_AGENT"), "start": time.time(),
             "code_hash": code_hash(src), "commit": commit, "cwd": os.getcwd(),
             "benchbook_run": env("AR_RUN"),
             "benchbook_run_id": env("AR_RUN_ID"),
             "seed": int(seed) if seed and seed.lstrip("-").isdigit() else None})
    print(rid)


def end(rid: str, code: str, log: str) -> None:
    try:
        text = open(log, encoding="utf-8", errors="replace").read()
    except OSError:
        text = ""
    m = parse_summary(text)
    killed = code == "killed"
    _append({"event": "end", "id": rid, "end": time.time(), "exit": None if killed else int(code), "killed": killed,
             "val_bpb": m.get("val_bpb"), "peak_vram_mb": m.get("peak_vram_mb"),
             "training_seconds": m.get("training_seconds")})


if __name__ == "__main__":
    if sys.argv[1] == "start":
        script = training_path(sys.argv[2], sys.argv[3:])
        if script is not None:
            start(script)
    else:
        end(sys.argv[2], sys.argv[3], sys.argv[4])
