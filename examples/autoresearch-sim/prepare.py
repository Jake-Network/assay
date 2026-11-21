"""Simulated autoresearch prepare.py (benchbook CPU simulator): the fixed harness constants only.

Like upstream, this file is read-only for agents. Its normalised hash is part of the partition, so changing
anything here (TIME_BUDGET, the evaluation) puts runs into a different comparison group.
"""

MAX_SEQ_LEN = 2048          # context length
TIME_BUDGET = 3             # simulated training time budget in seconds (upstream: 300)
EVAL_TOKENS = 40 * 524288   # number of tokens for val eval


def evaluate_bpb(loss: float) -> float:
    """The simulated ground truth: the loss is already in bits per byte."""
    return loss
