"""Token estimate without a tokenizer (spec §8.3): ~4 ASCII chars per token, 1 per non-ASCII char."""
from __future__ import annotations

import math


def estimate_tokens(text: str) -> int:
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return math.ceil(ascii_n / 4 + (len(text) - ascii_n) * 1.0)
