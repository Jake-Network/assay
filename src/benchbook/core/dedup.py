"""Canonical JSON, dedup keys and statement hashes (spec §9)."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any

from ..errors import ValidationFailed


def _norm(value: Any, path: str) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValidationFailed([f"{path or '$'}: NaN/Infinity is not allowed"])
        f = float(f"{value:.12g}")
        # JSON has one number type: 1.0 and 1 are the same config (DECISIONS.md)
        return int(f) if f.is_integer() and abs(f) < 2**53 else f
    if isinstance(value, dict):
        return {str(k): _norm(v, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v, f"{path}[{i}]") for i, v in enumerate(value)]
    raise ValidationFailed([f"{path or '$'}: unsupported type {type(value).__name__}"])


def canonical_json(value: Any) -> str:
    return json.dumps(_norm(value, ""), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _drop(cfg: dict[str, Any], ignore: list[str]) -> dict[str, Any]:
    """Remove ignored keys. A key is matched at the top level first; "a.b" also removes b inside object a."""
    top = set(ignore)
    kept = {k: v for k, v in cfg.items() if k not in top}
    nested: dict[str, list[str]] = {}
    for path in ignore:
        head, dot, rest = path.partition(".")
        if dot and rest and head in kept and isinstance(kept[head], dict):
            nested.setdefault(head, []).append(rest)
    for head, rests in nested.items():
        kept[head] = _drop(kept[head], rests)
    return kept


def dedup_key(config: dict[str, Any], code_ref: str | None, ignore: list[str]) -> str:
    kept = _drop(config, list(ignore))
    raw = canonical_json(kept) + "\n" + (code_ref or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_PUNCT_EDGE = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def normalize_statement(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = " ".join(s.split())
    return _PUNCT_EDGE.sub("", s)


def statement_hash(s: str) -> str:
    return hashlib.sha256(normalize_statement(s).encode("utf-8")).hexdigest()
