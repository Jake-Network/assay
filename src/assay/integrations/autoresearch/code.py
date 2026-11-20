"""train.py as an experiment: normalised code hash, knob extraction and knob diffs (§6.1).

Pure functions over source text; nothing here touches the ledger or the file system.
"""
from __future__ import annotations

import ast
import hashlib
import math
import operator
import re
from typing import Any

KNOB_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
DEFAULT_KNOB_IGNORE = ["H100_BF16_PEAK_FLOPS"]
_MAX_INT_BITS = 256
_MAX_STR = 10_000
_MAX_SEQ = 1_000


def _text(src: str | bytes) -> str | None:
    if isinstance(src, str):
        return src
    try:
        return src.decode("utf-8")
    except UnicodeDecodeError:
        return None


def normalize(src: str | bytes) -> str | None:
    """ast.unparse(ast.parse(src)): comments, blank lines, spacing and quote style disappear. For display
    only: its output differs between Python versions, so code_hash does not use it. None when it does not parse."""
    text = _text(src)
    if text is None:
        return None
    try:
        return ast.unparse(ast.parse(text))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _dump(node: Any, out: list[str]) -> None:
    """A serialisation of the syntax tree that does not depend on the Python version (A7): node names and
    non-empty fields only (fields added by newer versions are empty for code older versions can parse), no
    positions, constants as type:repr, adjacent string parts of f-strings merged."""
    if isinstance(node, ast.AST):
        out.append(type(node).__name__ + "(")
        for name in node._fields:
            if isinstance(node, ast.Constant) and name == "kind":
                continue  # u"" prefix
            v = getattr(node, name, None)
            if isinstance(node, ast.JoinedStr) and name == "values":
                v = _merge_str_parts(v or [])
            if v is None or (isinstance(v, list) and not v):
                continue
            out.append(name + "=")
            _dump(v, out)
            out.append(",")
        out.append(")")
    elif isinstance(node, list):
        out.append("[")
        for item in node:
            _dump(item, out)
            out.append(",")
        out.append("]")
    else:
        out.append(f"{type(node).__name__}:{node!r}")


def _merge_str_parts(values: list[Any]) -> list[Any]:
    merged: list[Any] = []
    for v in values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            if not v.value:
                continue
            if merged and isinstance(merged[-1], ast.Constant) and isinstance(merged[-1].value, str):
                merged[-1] = ast.Constant(merged[-1].value + v.value)
                continue
        merged.append(v)
    return merged


def canonical_source(src: str | bytes) -> str | None:
    """The version-independent form that code_hash hashes. None when the source does not parse."""
    text = _text(src)
    if text is None:
        return None
    try:
        tree = ast.parse(text)
        out: list[str] = []
        _dump(tree, out)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    return "".join(out)


def code_hash(src: str | bytes) -> str:
    """sha256 of the canonical syntax tree (comments, blank lines, spacing and quote style ignored, the same on
    every supported Python version); 'raw-' + sha256 of the bytes when it does not parse."""
    norm = canonical_source(src)
    if norm is not None:
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()
    raw = src.encode("utf-8") if isinstance(src, str) else src
    return "raw-" + hashlib.sha256(raw).hexdigest()


def code_hashes(src: str | bytes) -> tuple[str, ...]:
    """Canonical identity followed by the pre-canonical identity, when different."""
    current = code_hash(src)
    legacy = normalize(src)
    if legacy is None:
        return (current,)
    previous = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    return (current, previous) if current != previous else (current,)


def short_hash(h: str | None, n: int = 8) -> str:
    if not h:
        return "-" * n
    return ("raw-" + h[4:4 + n]) if h.startswith("raw-") else h[:n]


class _Unsupported(Exception):
    pass


_BINOPS: dict[type, Any] = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                            ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Pow: operator.pow,
                            ast.Mod: operator.mod}


def _check(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        if v.bit_length() > _MAX_INT_BITS:
            raise _Unsupported
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            raise _Unsupported
        return v
    if isinstance(v, str):
        if len(v) > _MAX_STR:
            raise _Unsupported
        return v
    if isinstance(v, list):
        if len(v) > _MAX_SEQ:
            raise _Unsupported
        return v
    raise _Unsupported


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def safe_eval(node: ast.AST) -> Any:
    """Constants, tuples/lists of them, unary +/-, and + - * / // ** % arithmetic. Anything else raises."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (bool, int, float, str)) or node.value is None:
            return _check(node.value)
        raise _Unsupported
    if isinstance(node, (ast.Tuple, ast.List)):
        if len(node.elts) > _MAX_SEQ:
            raise _Unsupported
        return [safe_eval(e) for e in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = safe_eval(node.operand)
        if not _num(v):
            raise _Unsupported
        return _check(-v if isinstance(node.op, ast.USub) else +v)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        a, b = safe_eval(node.left), safe_eval(node.right)
        if isinstance(node.op, ast.Pow):
            if not (_num(a) and _num(b)) or abs(b) > 64 or (isinstance(a, int) and abs(a) > 2**32 and b > 4):
                raise _Unsupported
        elif isinstance(node.op, ast.Mult) and (isinstance(a, str) or isinstance(b, str)):
            n = b if isinstance(a, str) else a
            s = a if isinstance(a, str) else b
            if not isinstance(n, int) or isinstance(n, bool) or len(s) * max(n, 0) > _MAX_STR:
                raise _Unsupported
        elif not (_num(a) and _num(b)) and not (isinstance(node.op, ast.Add) and type(a) is type(b)
                                                  and isinstance(a, (str, list))):
            raise _Unsupported
        try:
            return _check(_BINOPS[type(node.op)](a, b))
        except (ArithmeticError, ValueError, TypeError) as e:
            raise _Unsupported from e
    raise _Unsupported


def extract_knobs(src: str | bytes) -> dict[str, Any]:
    """Every module-level `NAME = value` whose name is UPPER_CASE (§6.1.1). Values the safe evaluator
    cannot compute are kept as {"expr": source}. A file that does not parse yields {}."""
    text = _text(src)
    if text is None:
        return {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return {}
    knobs: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not KNOB_NAME.match(target.id):
            continue
        try:
            knobs[target.id] = safe_eval(node.value)
        except (_Unsupported, RecursionError):
            knobs[target.id] = {"expr": ast.unparse(node.value)}
    return knobs


def _same(a: Any, b: Any) -> bool:
    if _num(a) and _num(b):
        return float(a) == float(b)
    return bool(a == b and type(a) is type(b)) or (isinstance(a, list) and isinstance(b, list) and a == b)


def changed_knobs(parent: dict[str, Any] | None, child: dict[str, Any],
                  ignore: list[str] | None = None) -> dict[str, list[Any]]:
    """{"MATRIX_LR": [0.04, 0.06]} for every knob whose value differs from the parent (None = absent)."""
    if parent is None:
        return {}
    skip = set(DEFAULT_KNOB_IGNORE if ignore is None else ignore)
    out: dict[str, list[Any]] = {}
    for k in sorted(set(parent) | set(child)):
        if k in skip:
            continue
        a, b = parent.get(k), child.get(k)
        if not _same(a, b):
            out[k] = [a, b]
    return out


def parse_knob_value(raw: str) -> Any:
    """CLI values: Python literals where possible (0.06, 2**19, (0.8, 0.95), "SSSL"), else the string."""
    try:
        node = ast.parse(raw.strip(), mode="eval").body
        v = safe_eval(node)
        return v
    except (SyntaxError, ValueError, _Unsupported, RecursionError):
        return raw.strip()


def knob_text(v: Any) -> str:
    if isinstance(v, dict) and set(v) == {"expr"}:
        return str(v["expr"])
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "(" + ", ".join(knob_text(x) for x in v) + ")"
    return str(v)
