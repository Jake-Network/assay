"""autoresearch integration: pure functions (no database). §16.1."""
from __future__ import annotations

from pathlib import Path

import pytest

from assay.integrations.autoresearch.code import (
    changed_knobs,
    code_hash,
    extract_knobs,
    knob_text,
    normalize,
    parse_knob_value,
)
from assay.integrations.autoresearch.hw import detect, gpu_class
from assay.integrations.autoresearch.runner import parse_summary, results_tsv_line

FIXTURE = Path(__file__).parent / "fixtures" / "autoresearch" / "upstream_train_constants.py"
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
    from assay.integrations.autoresearch import hw

    assert detect("My Box 1") == "my-box-1"
    assert detect(env={"ASSAY_AR_HW": "H100 80GB"}) == "h100-80gb"
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
