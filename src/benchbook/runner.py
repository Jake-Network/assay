"""The check runner: one shell command in its own process group, with a log file, a timeout and an exit code.

Core knows nothing about what the command is. The autoresearch integration runs a training through it and asks
for a metric in the summary; `benchbook check` runs a declared check through it with `metric=None` and `tee=True`."""
from __future__ import annotations

import codecs
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

DEFAULT_PARSE_REGEX = r"^(\w+):\s+([-\d.eE+]+)\s*$"
_INT = re.compile(r"^[-+]?\d+$")
_CHUNK = 65536
# How long tee keeps copying after the command is gone. The pipe normally reaches end-of-file at once; it stays
# open only when the command left a background process holding it, and a result must not wait for that one.
_TEE_DRAIN_S = 2.0


@dataclass
class RunResult:
    run_status: str  # ok | error | timeout
    exit_code: int | None  # None: timed out (the group was killed) or interrupted
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


class Interrupted(BaseException):  # noqa: N818 - like KeyboardInterrupt
    """SIGTERM or SIGHUP reached the runner while the command ran (A4)."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(signal.Signals(signum).name)


def _child_dies_with_parent() -> Callable[[], None] | None:
    """Linux: the command gets SIGTERM when the runner dies, even by SIGKILL (PR_SET_PDEATHSIG)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None

    def preexec() -> None:
        prctl(1, int(signal.SIGTERM))

    return preexec


def tail(text: str, lines: int = 30, chars: int = 1500) -> str:
    """The last `lines` lines, cut to their last `chars` characters. Long lines cost the start, never the end:
    the final line is the one that names the failure."""
    return "\n".join(text.replace("\r", "\n").splitlines()[-lines:])[-chars:]


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


def _trap_signals() -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}

    def raise_interrupted(signum: int, frame: object) -> None:
        raise Interrupted(signum)

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, raise_interrupted)
    return previous


def _stdout_writer() -> Callable[[bytes], None] | None:
    """Writes bytes to this process's stdout as they are: through the binary layer, so no newline translation and
    no decoding. A replaced sys.stdout without one (a StringIO) gets the text, decoded across chunk borders."""
    out = sys.stdout
    if out is None:
        return None
    try:
        out.flush()  # what the caller printed before the command stays before the command's output
    except (OSError, ValueError):
        return None
    raw = getattr(out, "buffer", None)
    if raw is not None:
        def write(chunk: bytes) -> None:
            raw.write(chunk)
            raw.flush()

        return write
    decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode

    def write_text(chunk: bytes) -> None:
        out.write(decode(chunk))
        out.flush()

    return write_text


class _Tee:
    """Copies the command's output to the log file and to stdout, chunk by chunk as it arrives.

    A thread and blocking reads, because a pipe cannot be polled on Windows."""

    def __init__(self, src: IO[bytes], log: IO[bytes]):
        self._src = src
        self._log = log
        self._write_out = _stdout_writer()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._copy, name="benchbook-tee", daemon=True)
        self._thread.start()

    def _copy(self) -> None:
        write_out = self._write_out
        try:
            # An unbuffered pipe: read() returns what is there, so a line shows up when the command prints it.
            while chunk := self._src.read(_CHUNK):
                if self._stopped.is_set():
                    return
                self._log.write(chunk)
                self._log.flush()
                if write_out is not None:
                    try:
                        write_out(chunk)
                    except (OSError, ValueError):
                        write_out = None  # stdout went away (`| head`): the log still gets everything
        except (OSError, ValueError):
            pass  # the log was closed under a copy that outlived its run
        finally:
            self._src.close()

    def finish(self) -> None:
        """Wait for the end of the output. Past the drain time the rest is dropped: whatever still holds the
        pipe is a process the command left behind, and its next write fails once the copy has closed the pipe."""
        self._thread.join(_TEE_DRAIN_S)
        self._stopped.set()


def run_command(cmd: str, cwd: Path, log_path: Path, timeout: float, metric: str | None = None,
                parse_regex: str = DEFAULT_PARSE_REGEX, env: dict[str, str] | None = None,
                tee: bool = False) -> RunResult:
    """Run `cmd` in its own process group with stdout+stderr redirected to log_path. On timeout the
    whole group is killed. ok needs exit code 0 and, when `metric` is given, that metric in the summary.

    `tee` also passes the output through to this process's stdout while the command runs: the same bytes in the
    same order as `cmd 2>&1` (stderr is folded into stdout, which is what keeps the order).

    The group is also killed when the runner is interrupted: Ctrl-C raises KeyboardInterrupt, SIGTERM and
    SIGHUP raise Interrupted (both re-raised after the kill, so the caller can record the run), and on Linux
    the command gets SIGTERM if the runner is killed outright."""
    kw: dict[str, object] = {}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kw["start_new_session"] = True
        if (pre := _child_dies_with_parent()) is not None:
            kw["preexec_fn"] = pre
    if tee:
        kw["bufsize"] = 0
    t0 = time.monotonic()
    timed_out = False
    previous = _trap_signals()
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=subprocess.PIPE if tee else log,  # noqa: S602
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                                    **kw)  # type: ignore[call-overload]
            copy = _Tee(proc.stdout, log) if tee else None
            try:
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(proc)
                except BaseException:
                    _kill_group(proc)
                    raise
            finally:
                if copy is not None:
                    copy.finish()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    duration_ms = int((time.monotonic() - t0) * 1000)
    text = log_path.read_bytes().decode("utf-8", errors="replace")
    metrics = parse_summary(text, parse_regex)
    if timed_out:
        return RunResult("timeout", None, metrics, duration_ms, tail(text), f"timeout after {timeout:g}s")
    if proc.returncode != 0:
        return RunResult("error", proc.returncode, metrics, duration_ms, tail(text), f"exit {proc.returncode}")
    if metric is not None and metric not in metrics:
        return RunResult("error", 0, metrics, duration_ms, tail(text), f"no {metric} in the summary")
    return RunResult("ok", 0, metrics, duration_ms, tail(text), "")
