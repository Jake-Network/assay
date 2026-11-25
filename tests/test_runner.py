"""The core runner: exit codes, the timeout that kills the process group, tee and interruption. No database."""
from __future__ import annotations

import io
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from benchbook.core import runner
from benchbook.core.runner import Interrupted, RunResult, run_command

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and process groups")


def _py(tmp_path: Path, name: str, body: str) -> str:
    """A shell command that runs `body` as a script (a file, so no quoting differs between shells)."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    quote = subprocess.list2cmdline if sys.platform == "win32" else shlex.join
    return quote([sys.executable, str(path)])


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # Still in the process table: an orphan that nobody has reaped yet (a container without an init) is dead too.
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout
    return not stat.strip() or stat.strip().startswith("Z")


def _wait_gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


def _wait_for(path: Path, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not (path.exists() and path.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.02)


# The command is a shell that starts another shell that starts sleep: killing only the command's own process
# would leave the inner shell and its sleep (the grandchild) running.
TREE = "sh -c 'sleep 120 & echo $! > grandchild; echo started; wait' & echo $! > child; wait"


class _Stdout:
    """A stand-in for sys.stdout that keeps the bytes written to its binary layer."""

    def __init__(self) -> None:
        self.buffer = self
        self.data = b""

    def write(self, chunk: bytes) -> int:
        self.data += bytes(chunk)
        return len(chunk)

    def flush(self) -> None:
        pass


def test_exit_code_alone_decides_without_a_metric(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    res = run_command(_py(tmp_path, "ok.py", "print('checked 3 files')\n"), tmp_path, log, 60)
    assert (res.run_status, res.exit_code, res.reason) == ("ok", 0, "")
    assert res.log_tail.strip() == "checked 3 files" and res.metrics == {}
    assert log.read_text().strip() == "checked 3 files"

    fail = _py(tmp_path, "fail.py", "import sys\nprint('2 passed, 1 failed', flush=True)\n"
                                    "print('boom', file=sys.stderr)\nsys.exit(3)\n")
    res = run_command(fail, tmp_path, log, 60, metric=None)
    assert (res.run_status, res.exit_code, res.reason) == ("error", 3, "exit 3")
    assert res.log_tail.split() == ["2", "passed,", "1", "failed", "boom"]  # stderr is in the log, in order


def test_a_named_metric_must_be_in_the_summary(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    cmd = _py(tmp_path, "score.py", "print('score: 2.5')\nprint('steps: 10')\n")
    res = run_command(cmd, tmp_path, log, 60, metric="score")
    assert (res.run_status, res.exit_code, res.metrics) == ("ok", 0, {"score": 2.5, "steps": 10})
    res = run_command(cmd, tmp_path, log, 60, metric="val_loss")
    assert (res.run_status, res.exit_code, res.reason) == ("error", 0, "no val_loss in the summary")
    assert run_command(cmd, tmp_path, log, 60).metrics == {"score": 2.5, "steps": 10}  # parsed either way


def test_log_tail_keeps_the_end_of_long_output(tmp_path: Path) -> None:
    """Thirty long lines are more than the tail holds; the last line is what a failure signature is made from."""
    lines = [f"FAILED tests/test_media.py::test_case_{i} - AssertionError: {'x' * 80}" for i in range(40)]
    text = "\n".join([*lines, "E   ValueError: the real reason", "=== 40 failed in 1.23s ==="])
    assert runner.tail("a\rb\nc", lines=2) == "b\nc" and runner.tail("short") == "short"
    kept = runner.tail(text)
    assert len(kept) == 1500 and kept.endswith("E   ValueError: the real reason\n=== 40 failed in 1.23s ===")
    cmd = _py(tmp_path, "long.py", f"import sys\nsys.stdout.buffer.write({text.encode()!r})\nsys.exit(1)\n")
    res = run_command(cmd, tmp_path, tmp_path / "run.log", 60)
    assert res.exit_code == 1 and res.log_tail == kept


def test_result_carries_the_duration(tmp_path: Path) -> None:
    res = run_command(_py(tmp_path, "slow.py", "import time\ntime.sleep(0.4)\n"), tmp_path, tmp_path / "run.log", 60)
    assert res.run_status == "ok" and 400 <= res.duration_ms < 60_000


def test_timeout_is_reported_with_the_output_so_far(tmp_path: Path) -> None:
    cmd = _py(tmp_path, "hang.py", "import time\nprint('collecting', flush=True)\ntime.sleep(120)\n")
    t0 = time.monotonic()
    res = run_command(cmd, tmp_path, tmp_path / "run.log", 2)
    assert time.monotonic() - t0 < 60
    assert (res.run_status, res.exit_code, res.reason) == ("timeout", None, "timeout after 2s")
    assert res.log_tail.strip() == "collecting" and res.duration_ms >= 2000


@posix_only
@pytest.mark.parametrize("tee", [False, True])
def test_timeout_kills_the_whole_process_group(tmp_path: Path, tee: bool) -> None:
    t0 = time.monotonic()
    res = run_command(TREE, tmp_path, tmp_path / "run.log", 2, tee=tee)
    assert time.monotonic() - t0 < 60
    assert (res.run_status, res.exit_code) == ("timeout", None)
    assert res.log_tail == "started"
    child, grandchild = (int((tmp_path / name).read_text()) for name in ("child", "grandchild"))
    assert _wait_gone(child) and _wait_gone(grandchild)


@posix_only
@pytest.mark.parametrize("tee", [False, True])
def test_sigterm_raises_interrupted_and_leaves_no_child(tmp_path: Path, tee: bool) -> None:
    before = signal.getsignal(signal.SIGTERM)

    def terminate_once_started() -> None:
        _wait_for(tmp_path / "grandchild")
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=terminate_once_started, daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(Interrupted) as e:
        run_command(TREE, tmp_path, tmp_path / "run.log", 60, tee=tee)
    assert time.monotonic() - t0 < 30
    assert e.value.signum == signal.SIGTERM and e.value.args == ("SIGTERM",)
    assert signal.getsignal(signal.SIGTERM) is before  # the runner's handler does not outlive the run
    child, grandchild = (int((tmp_path / name).read_text()) for name in ("child", "grandchild"))
    assert _wait_gone(child) and _wait_gone(grandchild)
    assert (tmp_path / "run.log").read_text() == "started\n"  # what a caller records the interrupted run from


EMIT = """import sys
out, err = sys.stdout.buffer, sys.stderr.buffer
out.write(b"plain line\\n"); out.flush()
err.write(b"warning on stderr\\r\\n"); err.flush()
out.write(b"not utf-8: \\xff\\xfe nul: \\x00 tab:\\t|\\n"); out.flush()
out.write(bytes(range(256)) * 1200); out.flush()
err.write(b"\\x1b[31mFAILED\\x1b[0m tests/test_x.py::test_y\\n"); err.flush()
out.write(b"last line without a newline"); out.flush()
sys.exit(7)
"""

DRIVER = """import json, sys
from pathlib import Path
from benchbook.core.runner import run_command

cmd, cwd = sys.argv[1], Path(sys.argv[2])
sys.stdout.write("before|")  # left in the text layer on purpose: the runner must flush it first
res = run_command(cmd, cwd, cwd / "tee.log", 60, tee=True)
sys.stdout.write("|after")
(cwd / "result.json").write_text(json.dumps([res.run_status, res.exit_code, res.reason]))
"""


def test_tee_output_equals_the_plain_command(tmp_path: Path) -> None:
    cmd = _py(tmp_path, "emit.py", EMIT)
    plain = subprocess.run(cmd, shell=True, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,  # noqa: S602
                           stdin=subprocess.DEVNULL, check=False)
    assert plain.returncode == 7 and len(plain.stdout) > 300_000
    (tmp_path / "driver.py").write_text(DRIVER, encoding="utf-8")
    teed = subprocess.run([sys.executable, str(tmp_path / "driver.py"), cmd, str(tmp_path)], stdout=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, check=True)
    assert teed.stdout == b"before|" + plain.stdout + b"|after"
    assert (tmp_path / "tee.log").read_bytes() == plain.stdout
    assert json.loads((tmp_path / "result.json").read_text()) == ["error", 7, "exit 7"]


def test_log_is_the_same_with_and_without_tee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = _py(tmp_path, "emit.py", EMIT)
    quiet = run_command(cmd, tmp_path, tmp_path / "quiet.log", 60)
    out = _Stdout()
    monkeypatch.setattr(sys, "stdout", out)
    loud = run_command(cmd, tmp_path, tmp_path / "loud.log", 60, tee=True)
    monkeypatch.undo()
    assert out.data == (tmp_path / "loud.log").read_bytes() == (tmp_path / "quiet.log").read_bytes()
    assert (loud.run_status, loud.exit_code, loud.log_tail) == (quiet.run_status, quiet.exit_code, quiet.log_tail)


def test_tee_passes_output_through_while_the_command_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command prints, then waits until the test has seen that line on stdout. A tee that held the output
    until the command ended would never let it see the line, and the command would give up with exit 3."""
    seen = tmp_path / "seen"
    cmd = _py(tmp_path, "wait.py", f"""import sys, time
from pathlib import Path
sys.stdout.buffer.write(b"first\\n"); sys.stdout.buffer.flush()
deadline = time.monotonic() + 20
while not Path({str(seen)!r}).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
if not Path({str(seen)!r}).exists():
    sys.exit(3)
sys.stdout.buffer.write(b"second\\n")
""")

    class Watching(_Stdout):
        def write(self, chunk: bytes) -> int:
            n = super().write(chunk)
            if b"first" in self.data:
                seen.touch()
            return n

    out = Watching()
    monkeypatch.setattr(sys, "stdout", out)
    res = run_command(cmd, tmp_path, tmp_path / "run.log", 60, tee=True)
    monkeypatch.undo()
    assert (res.run_status, res.exit_code) == ("ok", 0)
    assert out.data == b"first\nsecond\n"


def test_tee_to_a_text_only_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replaced sys.stdout without a binary layer gets text; a character split over two writes stays whole."""
    cmd = _py(tmp_path, "split.py", """import sys, time
out = sys.stdout.buffer
out.write(b"caf\\xc3"); out.flush()
time.sleep(0.3)
out.write(b"\\xa9\\n"); out.flush()
""")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    res = run_command(cmd, tmp_path, tmp_path / "run.log", 60, tee=True)
    monkeypatch.undo()
    assert res.run_status == "ok" and out.getvalue() == "café\n"
    assert (tmp_path / "run.log").read_bytes() == b"caf\xc3\xa9\n"


def test_tee_keeps_the_log_when_stdout_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Closed(_Stdout):
        def write(self, chunk: bytes) -> int:
            raise BrokenPipeError

    monkeypatch.setattr(sys, "stdout", Closed())
    res = run_command(_py(tmp_path, "emit.py", EMIT), tmp_path, tmp_path / "run.log", 60, tee=True)
    monkeypatch.undo()
    assert (res.run_status, res.exit_code) == ("error", 7)
    assert (tmp_path / "run.log").read_bytes().endswith(b"last line without a newline")


def test_tee_waits_for_a_slow_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The command is gone while the copy is still writing its first line: the drain time is for a pipe that
    stays silent, not for a reader that is slow, so the second line is not dropped."""
    cmd = _py(tmp_path, "two.py", """import sys, time
out = sys.stdout.buffer
out.write(b"one\\n"); out.flush()
time.sleep(0.5)
out.write(b"two\\n"); out.flush()
""")

    class Slow(_Stdout):
        def write(self, chunk: bytes) -> int:
            time.sleep(1.5)
            return super().write(chunk)

    out = Slow()
    monkeypatch.setattr(runner, "_TEE_DRAIN_S", 0.2)
    monkeypatch.setattr(sys, "stdout", out)
    res = run_command(cmd, tmp_path, tmp_path / "run.log", 60, tee=True)
    monkeypatch.undo()
    assert res.run_status == "ok"
    assert out.data == (tmp_path / "run.log").read_bytes() == b"one\ntwo\n"


def test_tee_copies_the_output_that_arrives_as_the_command_ends(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """A command that was silent for longer than the drain time prints its last line and exits. The copy has not
    read that line yet when the runner asks it to finish; the silence before must not count against it."""

    class Pipe:
        def __init__(self) -> None:
            self.release = threading.Event()
            self.chunks = [b"last words\n", b""]

        def read(self, n: int) -> bytes:
            if len(self.chunks) == 2:
                self.release.wait(10)
            return self.chunks.pop(0)

        def close(self) -> None:
            pass

    pipe, out = Pipe(), _Stdout()
    monkeypatch.setattr(runner, "_TEE_DRAIN_S", 0.5)
    monkeypatch.setattr(sys, "stdout", out)
    with open(tmp_path / "run.log", "wb") as log:
        copy = runner._Tee(pipe, log)  # type: ignore[arg-type]
        time.sleep(0.8)
        threading.Timer(0.1, pipe.release.set).start()
        copy.finish()
    monkeypatch.undo()
    assert out.data == (tmp_path / "run.log").read_bytes() == b"last words\n"


@posix_only
def test_tee_does_not_wait_for_a_background_process_that_holds_the_pipe(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_TEE_DRAIN_S", 0.5)
    monkeypatch.setattr(sys, "stdout", _Stdout())
    t0 = time.monotonic()
    try:
        res = run_command("sleep 60 & echo $! > straggler; echo done", tmp_path, tmp_path / "run.log", 60, tee=True)
    finally:
        monkeypatch.undo()
        _wait_for(tmp_path / "straggler")
        os.kill(int((tmp_path / "straggler").read_text()), signal.SIGKILL)
    assert time.monotonic() - t0 < 30
    assert (res.run_status, res.exit_code, res.log_tail) == ("ok", 0, "done")


def test_autoresearch_runner_reexports_core_and_keeps_its_metric(tmp_path: Path) -> None:
    from benchbook.integrations.autoresearch import runner as ar

    for name in ("RunResult", "Interrupted", "parse_summary", "tail", "DEFAULT_PARSE_REGEX", "_kill_group",
                 "_trap_signals", "_child_dies_with_parent"):
        assert getattr(ar, name) is getattr(runner, name), name
    assert RunResult("ok", 0).log_tail == ""
    cmd = _py(tmp_path, "train.py", "print('val_loss: 1.0')\n")
    res = ar.run_command(cmd, tmp_path, tmp_path / "run.log", 60)  # a training without val_bpb is not ok
    assert (res.run_status, res.reason) == ("error", "no val_bpb in the summary")
    assert run_command(cmd, tmp_path, tmp_path / "run.log", 60).run_status == "ok"
