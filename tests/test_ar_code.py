"""autoresearch integration: pure functions (no database). §16.1."""
from __future__ import annotations

from pathlib import Path

import pytest

from benchbook.integrations.autoresearch.code import (
    changed_knobs,
    code_hash,
    extract_knobs,
    knob_text,
    normalize,
    parse_knob_value,
)
from benchbook.integrations.autoresearch.hw import detect, gpu_class
from benchbook.integrations.autoresearch.runner import parse_summary, results_tsv_line

FIXTURE = Path(__file__).parent / "fixtures" / "autoresearch" / "upstream_train_constants.py"
GOLDEN_FSTRING = "56e5ad786e5caee23ae497b9515d53cca958efd6f7aa7a25d345e59b3c7e0b9b"
GOLDEN_FIXTURE = "7d0bfbf6f4e71f916aa77ddd3c20a4a214b80241bddcb998cf4f438d37921502"
UPSTREAM_SUMMARY = """step 00953 (100.0%) | loss: 3.012 | lrm: 0.00 | dt: 310ms | tok/sec: 1,690,000 | mfu: 39.8%
Gradient accumulation steps: 4
Time budget: 300s
---
val_bpb:          0.997900
training_seconds: 300.1
total_seconds:    325.9
peak_vram_mb:     45060.2
mfu_percent:      39.80
total_tokens_M:   499.6
num_steps:        953
num_params_M:     50.3
depth:            8
"""


def test_hash_ignores_comments_spacing_and_quotes() -> None:
    a = "X = 1  # one\nNAME = 'ab'\n\n\ndef f(a,b):\n    return a+b\n"
    b = '# header comment\nX=1\nNAME = "ab"\ndef f(a, b):\n    return a + b  # sum\n'
    assert code_hash(a) == code_hash(b)
    assert code_hash(a) != code_hash(a.replace("X = 1", "X = 2"))
    assert normalize("def (") is None
    assert code_hash("def (").startswith("raw-") and code_hash(b"\xff\xfe").startswith("raw-")


def test_upstream_knobs() -> None:
    knobs = extract_knobs(FIXTURE.read_text())
    assert knobs == {
        "ASPECT_RATIO": 64, "HEAD_DIM": 128, "WINDOW_PATTERN": "SSSL", "TOTAL_BATCH_SIZE": 524288,
        "EMBEDDING_LR": 0.6, "UNEMBEDDING_LR": 0.004, "MATRIX_LR": 0.04, "SCALAR_LR": 0.5, "WEIGHT_DECAY": 0.2,
        "ADAM_BETAS": [0.8, 0.95], "WARMUP_RATIO": 0.0, "WARMDOWN_RATIO": 0.5, "FINAL_LR_FRAC": 0.0, "DEPTH": 8,
        "DEVICE_BATCH_SIZE": 128, "H100_BF16_PEAK_FLOPS": 989.5e12,
    }
    assert len(knobs) == 16


@pytest.mark.parametrize("src, expected", [
    ("A = -3 * (2 + 1)", -9),
    ("A = 7 // 2 + 7 % 2", 4),
    ("A = [1, 'x', None, True]", [1, "x", None, True]),
    ("A = 'S' * 3 + 'L'", "SSSL"),
    ("A = os.getenv('X')", {"expr": "os.getenv('X')"}),
    ("A = 10 ** 10 ** 10", {"expr": "10 ** 10 ** 10"}),
    ("A = 1 / 0", {"expr": "1 / 0"}),
    ("A = float('inf')", {"expr": "float('inf')"}),
    ("A = 'x' * 100000", {"expr": "'x' * 100000"}),
])
def test_safe_evaluator(src: str, expected: object) -> None:
    assert extract_knobs(src)["A"] == expected


def test_only_module_level_upper_case_single_targets() -> None:
    src = "lower = 1\nA, B = 1, 2\nC = D = 3\nE: int = 4\nif True:\n    F = 5\nG = 6\nG = 7\n"
    assert extract_knobs(src) == {"G": 7}
    assert extract_knobs("def (") == {}


def test_changed_knobs() -> None:
    parent = {"MATRIX_LR": 0.04, "DEPTH": 8, "H100_BF16_PEAK_FLOPS": 1.0, "OLD": 1}
    child = {"MATRIX_LR": 0.06, "DEPTH": 8.0, "H100_BF16_PEAK_FLOPS": 2.0, "NEW": "x"}
    assert changed_knobs(parent, child) == {"MATRIX_LR": [0.04, 0.06], "NEW": [None, "x"], "OLD": [1, None]}
    assert changed_knobs(None, child) == {}
    assert "H100_BF16_PEAK_FLOPS" in changed_knobs(parent, child, ignore=[])


def test_knob_values_from_cli() -> None:
    assert parse_knob_value("0.06") == 0.06
    assert parse_knob_value("2**19") == 524288
    assert parse_knob_value("(0.8, 0.95)") == [0.8, 0.95]
    assert parse_knob_value('"SSSL"') == "SSSL" and parse_knob_value("SSSL") == "SSSL"
    assert knob_text([0.8, 0.95]) == "(0.8, 0.95)" and knob_text(0.06) == "0.06"


@pytest.mark.parametrize("name, mib, expected", [
    ("NVIDIA H100 80GB HBM3", 81559, "h100-80gb"),
    ("NVIDIA H100 PCIe", 81559, "h100-pcie-80gb"),
    ("NVIDIA A100-SXM4-40GB", 40960, "a100-sxm4-40gb"),
    ("NVIDIA GeForce RTX 4090", 24564, "rtx-4090-24gb"),
    ("Tesla V100-SXM2-16GB", 16160, "v100-sxm2-16gb"),
])
def test_gpu_class(name: str, mib: float, expected: str) -> None:
    assert gpu_class(name, mib) == expected


def test_hw_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from benchbook.integrations.autoresearch import hw

    assert detect("My Box 1") == "my-box-1"
    assert detect(env={"BENCHBOOK_AR_HW": "H100 80GB"}) == "h100-80gb"
    monkeypatch.setattr(hw, "detect_nvidia", lambda env=None: None)
    monkeypatch.setattr(hw, "detect_macos", lambda: None)
    assert detect(env={}).startswith("unknown-")
    assert gpu_class("NVIDIA GeForce RTX 3060 Laptop GPU", 6144) == "rtx-3060-laptop-6gb"


def test_parse_summary() -> None:
    m = parse_summary(UPSTREAM_SUMMARY)
    assert m == {"val_bpb": 0.9979, "training_seconds": 300.1, "total_seconds": 325.9, "peak_vram_mb": 45060.2,
                 "mfu_percent": 39.8, "total_tokens_M": 499.6, "num_steps": 953, "num_params_M": 50.3, "depth": 8}
    assert isinstance(m["num_steps"], int)
    assert parse_summary("loss: nan\nval_bpb: 1.0\n") == {"val_bpb": 1.0}
    assert parse_summary("score = 3\n", r"^(\w+) = (\d+)$") == {"score": 3}


def test_results_tsv_lines() -> None:
    assert results_tsv_line("a1b2c3d", 0.9979, 45060.2, "keep", "baseline") == \
        "a1b2c3d\t0.997900\t44.0\tkeep\tbaseline\n"
    assert results_tsv_line("d4e5f6g", None, None, "crash", "double width\t(OOM)\n") == \
        "d4e5f6g\t0.000000\t0.0\tcrash\tdouble width (OOM)\n"



def test_hash_is_the_same_on_every_python_version() -> None:
    """A7: ast.unparse writes f"{'a'}" on 3.11/3.13 but f'{'a'}' on 3.12, so it cannot be the hash input.
    These values were checked on CPython 3.11, 3.12 and 3.13; a change here breaks dedup across machines."""
    src = 'x = f"{a!r:>10} {\'q\'} {b=}"\ny = f\'a\' \'b\' f\'{x}\'\n'
    assert code_hash(src) == GOLDEN_FSTRING
    assert code_hash(FIXTURE.read_text()) == GOLDEN_FIXTURE
    assert code_hash(src) != code_hash(src.replace("q", "r"))


def test_agent_name_keeps_the_whole_host() -> None:
    from benchbook.integrations.autoresearch.ops import default_agent_name

    env = {"CUDA_VISIBLE_DEVICES": "1,2"}
    assert default_agent_name(env, "gpu-node-12") == "gpu1-gpu-node-12"
    assert default_agent_name(env, "gpu-node-12") != default_agent_name(env, "gpu-node-13")  # A6
    long_a, long_b = "x" * 45 + "a.example.com", "x" * 45 + "b.example.com"
    assert default_agent_name(env, long_a) != default_agent_name(env, long_b)
    assert len(default_agent_name(env, long_a)) <= 50


def test_failing_nvidia_smi_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A9: a transient nvidia-smi failure must not become an unknown-<host> partition."""
    import subprocess

    from benchbook.errors import BenchbookError
    from benchbook.integrations.autoresearch import hw

    calls: list[object] = []

    def failing(cmd: list[str], **kw: object) -> None:
        calls.append(cmd)
        raise subprocess.CalledProcessError(9, cmd, "", "Unable to determine the device handle")

    monkeypatch.setattr(hw.subprocess, "run", failing)
    monkeypatch.setattr(hw.time, "sleep", lambda s: None)
    with pytest.raises(BenchbookError, match="nvidia-smi failed"):
        hw.detect(env={})
    assert len(calls) == 3

    def missing(cmd: list[str], **kw: object) -> None:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(hw.subprocess, "run", missing)
    assert hw.detect_nvidia({}) is None


def test_budget_check() -> None:
    from benchbook.integrations.autoresearch.runner import RunResult, check_budget

    ok = RunResult("ok", 0, {"val_bpb": 1.0, "training_seconds": 300.4}, 330_000)
    assert check_budget(ok, 300, 30, 240) is ok
    long = RunResult("ok", 0, {"val_bpb": 1.0, "training_seconds": 540.0}, 560_000)
    bad = check_budget(long, 300, 30, 240)
    assert bad.run_status == "invalid" and "training_seconds 540" in bad.reason and "wall clock" in bad.reason
    lying = RunResult("ok", 0, {"val_bpb": 1.0, "training_seconds": 299.0}, 600_000)
    assert check_budget(lying, 300, 30, 240).run_status == "invalid"
    assert check_budget(long, None).run_status == "ok"


def test_sigterm_kills_the_training_and_raises(tmp_path: Path) -> None:
    """A4: SIGTERM to the runner kills the training process group and surfaces as Interrupted."""
    import os
    import signal
    import sys
    import threading
    import time

    from benchbook.integrations.autoresearch.runner import Interrupted, run_command

    if sys.platform == "win32":
        pytest.skip("POSIX signals")
    pidfile = tmp_path / "pid"
    cmd = f"{sys.executable} -c \"import os, time; open('pid', 'w').write(str(os.getpid())); time.sleep(60)\""
    threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    t0 = time.monotonic()
    with pytest.raises(Interrupted):
        run_command(cmd, tmp_path, tmp_path / "run.log", 60)
    assert time.monotonic() - t0 < 30
    child = int(pidfile.read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    assert signal.getsignal(signal.SIGTERM) is not None  # the previous handler is back
