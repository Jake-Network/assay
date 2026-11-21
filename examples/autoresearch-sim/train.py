"""Simulated autoresearch train.py (benchbook CPU simulator). No torch, no GPU.

Same UPPER_CASE knobs and the same end-of-run summary as upstream train.py, so `benchbook ar` treats it exactly
like the real thing. val_bpb is a smooth function of the knobs plus one interaction term plus Gaussian noise.

Differences from the real script, on purpose:
- The noise is drawn fresh on every run (sigma AR_SIM_NOISE, default 0.002). The real train.py fixes its seed
  to 42, so its run-to-run noise comes only from the time budget and GPU kernels and is much smaller.
- With probability AR_SIM_LUCKY (default 0.02) a run is "lucky" (-0.01) so replication has something to catch.
- DEPTH * TOTAL_BATCH_SIZE above 12 * 2**19 "runs out of memory" and exits with status 1.
- It sleeps AR_SIM_SECONDS (default TIME_BUDGET) instead of training.
"""
import math
import os
import random
import sys
import time

from prepare import TIME_BUDGET, evaluate_bpb

# ---------------------------------------------------------------------------
# Hyperparameters (edit these directly, no CLI flags needed)
# ---------------------------------------------------------------------------

# Model architecture
ASPECT_RATIO = 64       # model_dim = depth * ASPECT_RATIO
HEAD_DIM = 128          # target head dimension for attention
WINDOW_PATTERN = "SSSL" # sliding window pattern: L=full, S=half context

# Optimization
TOTAL_BATCH_SIZE = 2**19 # ~524K tokens per optimizer step
EMBEDDING_LR = 0.6      # learning rate for token embeddings (Adam)
UNEMBEDDING_LR = 0.004  # learning rate for lm_head (Adam)
MATRIX_LR = 0.04        # learning rate for matrix parameters (Muon)
SCALAR_LR = 0.5         # learning rate for per-layer scalars (Adam)
WEIGHT_DECAY = 0.2      # cautious weight decay for Muon
ADAM_BETAS = (0.8, 0.95) # Adam beta1, beta2
WARMUP_RATIO = 0.0      # fraction of time budget for LR warmup
WARMDOWN_RATIO = 0.5    # fraction of time budget for LR warmdown
FINAL_LR_FRAC = 0.0     # final LR as fraction of initial

# Model size
DEPTH = 8               # number of transformer layers
DEVICE_BATCH_SIZE = 128  # per-device batch size (reduce if OOM)

H100_BF16_PEAK_FLOPS = 989.5e12

WINDOW_EFFECT = {"SSSL": 0.0, "SSLL": -0.003, "SL": -0.002, "L": 0.004, "S": 0.01}


def true_loss() -> float:
    """Noise-free val_bpb: smooth in every knob, optimum near MATRIX_LR=0.06, WARMDOWN_RATIO=0.65, DEPTH=10."""
    lr = math.log(MATRIX_LR / 0.06)
    emb = math.log(EMBEDDING_LR / 0.9)
    wd = WARMDOWN_RATIO - 0.65
    depth = DEPTH - 10
    batch = math.log2(TOTAL_BATCH_SIZE / 2**18)
    v = (0.990 + 0.020 * lr**2 + 0.004 * emb**2 + 0.03 * wd**2 + 0.0008 * depth**2 + 0.003 * batch**2
         + 0.02 * (WEIGHT_DECAY - 0.1) ** 2 + 0.05 * WARMUP_RATIO**2 + WINDOW_EFFECT.get(WINDOW_PATTERN, 0.006))
    v += 0.015 * lr * wd  # the interaction term: learning rate and warmdown are not independent
    return v


def main() -> None:
    rng = random.SystemRandom()
    run_id = rng.randrange(1, 2**53)
    print(f"sim_run_id: {run_id}", flush=True)
    if path := os.environ.get("AR_SIM_INVOCATIONS"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{run_id}\n")
    print(f"Time budget: {TIME_BUDGET}s")
    t0 = time.monotonic()  # time.time() can jump (WSL, NTP) and training_seconds is checked against the budget
    if DEPTH * TOTAL_BATCH_SIZE > 12 * 2**19:
        time.sleep(0.05)
        print("Traceback (most recent call last):\n  File \"train.py\", line 1, in <module>")
        print("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB (simulated)")
        sys.exit(1)
    time.sleep(float(os.environ.get("AR_SIM_SECONDS", TIME_BUDGET)))
    truth = true_loss()
    lucky = rng.random() < float(os.environ.get("AR_SIM_LUCKY", "0.02"))
    val_bpb = evaluate_bpb(truth + rng.gauss(0.0, float(os.environ.get("AR_SIM_NOISE", "0.002")))
                           - (0.01 if lucky else 0.0))
    training_seconds = time.monotonic() - t0
    steps = int(950 * 2**19 / TOTAL_BATCH_SIZE * 8 / DEPTH)
    print(f"true_bpb: {truth:.6f}")
    print(f"lucky: {int(lucky)}")
    print("---")
    print(f"val_bpb:          {val_bpb:.6f}")
    print(f"training_seconds: {training_seconds:.1f}")
    print(f"total_seconds:    {time.monotonic() - t0 + 0.4:.1f}")
    print(f"peak_vram_mb:     {45060.2 * DEPTH / 8 * DEVICE_BATCH_SIZE / 128:.1f}")
    print(f"mfu_percent:      {39.80:.2f}")
    print(f"total_tokens_M:   {steps * TOTAL_BATCH_SIZE / 1e6:.1f}")
    print(f"num_steps:        {steps}")
    print(f"num_params_M:     {50.3 * DEPTH / 8:.1f}")
    print(f"depth:            {DEPTH}")


if __name__ == "__main__":
    main()
