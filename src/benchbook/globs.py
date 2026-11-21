"""Path globs over repository-relative paths: guard paths and path occupancy.

A pattern is matched against the whole path, '/'-separated, from the repository root (no implicit "anywhere" as in
.gitignore). Within one path segment `*` matches any run of characters, `?` one character, `[abc]`/`[a-z]` one of a
set (`[!...]` or `[^...]` negates) and a backslash escapes the next character; none of them crosses a '/'. A
segment that is exactly `**` matches zero or more whole segments, or one or more at the end (`tests/**` is every file
under tests/, not a file named tests); `**` inside a segment (`a**b`) is two `*`.

Python 3.11 has neither glob.translate nor PurePath.full_match (3.13), and fnmatch lets `*` cross '/', so this module
does its own matching. A pattern is kept as its list of segments rather than one regular expression: deciding
whether two glob sets can match a common path (path occupancy, overlap()) is then a walk over two segment lists that
only has to compare one segment pattern with another."""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache, lru_cache

GLOBSTAR = "**"


@dataclass(frozen=True)
class Glob:
    pattern: str
    segments: tuple[str, ...]  # GLOBSTAR, or a pattern for exactly one path segment
    _regexes: tuple[re.Pattern[str] | None, ...] = field(repr=False, compare=False)

    def match(self, path: str) -> bool:
        parts = tuple(normalise(path).split("/"))
        return bool(parts[0]) and _match(self.segments, self._regexes, parts, 0, 0, {})


def normalise(path: str) -> str:
    """Leading './' and surrounding '/' dropped. Backslashes are kept: git paths use '/', and in a pattern a backslash
    escapes."""
    p = path
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def segment_regex(segment: str) -> str:
    """The regular expression for one segment pattern (no '/', not GLOBSTAR)."""
    out: list[str] = []
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(segment[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and (end := _class_end(segment, i)) is not None:
            body = segment[i + 1:end]
            negate = body[:1] in ("!", "^")
            if negate:
                body = body[1:]
            body = body.replace("\\", "\\\\").replace("[", "\\[")
            out.append(f"[{'^' if negate else ''}{body}]")  # a segment holds no '/', so [^...] cannot cross one
            i = end + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _class_end(segment: str, start: int) -> int | None:
    """Index of the ']' closing the class opened at start, or None (then '[' is an ordinary character)."""
    j = start + 1
    if j < len(segment) and segment[j] in ("!", "^"):
        j += 1
    if j < len(segment) and segment[j] == "]":
        j += 1  # a leading ']' is a member, as in fnmatch
    while j < len(segment):
        if segment[j] == "]":
            return j
        j += 1
    return None


@cache
def compile_glob(pattern: str) -> Glob:
    p = normalise(pattern)
    if not p:
        raise ValueError("empty glob pattern")
    segments: list[str] = []
    for seg in p.split("/"):
        if seg == GLOBSTAR:
            if segments and segments[-1] == GLOBSTAR:
                continue  # a/**/**/b is a/**/b
            segments.append(GLOBSTAR)
        elif seg:  # a//b is a/b
            segments.append(seg)
    regexes = tuple(None if s == GLOBSTAR else re.compile(segment_regex(s), re.DOTALL) for s in segments)
    return Glob(pattern, tuple(segments), regexes)


def _match(segs: tuple[str, ...], res: tuple[re.Pattern[str] | None, ...], parts: tuple[str, ...], i: int, j: int,
           memo: dict[tuple[int, int], bool]) -> bool:
    key = (i, j)
    if key in memo:
        return memo[key]
    if i == len(segs):
        ok = j == len(parts)
    elif segs[i] == GLOBSTAR:
        if i == len(segs) - 1:
            ok = j < len(parts)  # a trailing ** needs at least one segment: tests/** is not the file "tests"
        else:
            ok = any(_match(segs, res, parts, i + 1, k, memo) for k in range(j, len(parts) + 1))
    else:
        rx = res[i]
        ok = j < len(parts) and rx is not None and rx.fullmatch(parts[j]) is not None \
            and _match(segs, res, parts, i + 1, j + 1, memo)
    memo[key] = ok
    return ok


def match(path: str, pattern: str) -> bool:
    return compile_glob(pattern).match(path)


def match_any(path: str, patterns: Iterable[str]) -> bool:
    return any(compile_glob(p).match(path) for p in patterns)


def matching(paths: Iterable[str], patterns: Sequence[str]) -> list[str]:
    """The paths that match at least one pattern, in their original order."""
    globs = [compile_glob(p) for p in patterns]
    return [p for p in paths if any(g.match(p) for g in globs)]


# ---------------------------------------------------------------------------------------------- overlap
#
# Two glob sets overlap when some path matches a pattern of each. Path occupancy (pieces.py) refuses a piece whose
# paths overlap a live one's, so the test must never miss a common path. It is exact: overlap() returns a path both
# patterns match, or None only when there is none. A file is a pattern too, escaped (escape()), so file-vs-file,
# file-vs-glob and glob-vs-glob are one question.


@dataclass(frozen=True)
class _Char:
    """A token that stands for exactly one character of a segment: a literal, '?' or a [...] class."""
    literal: str | None = None
    rx: re.Pattern[str] | None = None  # a class; None with no literal is '?' (or a class Python cannot compile)
    candidates: tuple[str, ...] = ()  # characters worth trying when looking for one both tokens accept

    def accepts(self, ch: str) -> bool:
        if ch == "/":
            return False  # a segment never holds one
        if self.literal is not None:
            return ch == self.literal
        return self.rx is None or self.rx.fullmatch(ch) is not None


_ANY = _Char()
_Token = _Char | None  # None is '*': any run of characters, the empty one included
_FALLBACK = ("a", "0", "_")


def escape(path: str) -> str:
    """The pattern that matches exactly this path (a file name may contain *, ?, [ or a backslash)."""
    return re.sub(r"([*?\[\\])", r"\\\1", path)


@lru_cache(maxsize=4096)
def _tokens(segment: str) -> tuple[_Token, ...]:
    """A segment pattern as tokens, read exactly as segment_regex reads it."""
    out: list[_Token] = []
    i, n = 0, len(segment)
    while i < n:
        c = segment[i]
        if c == "\\" and i + 1 < n:
            out.append(_Char(literal=segment[i + 1], candidates=(segment[i + 1],)))
            i += 2
            continue
        if c == "*":
            out.append(None)
        elif c == "?":
            out.append(_ANY)
        elif c == "[" and (end := _class_end(segment, i)) is not None:
            text = segment[i:end + 1]
            try:
                rx: re.Pattern[str] | None = re.compile(segment_regex(text), re.DOTALL)
            except re.error:
                rx = None  # e.g. [z-a]: compile_glob refuses such a pattern; here it may match anything
            # The characters a class names, and their neighbours: if two classes share a character, they share one
            # of these (an intersection of ranges starts at a named character or just after one).
            near = {o + d for o in map(ord, text[1:-1]) for d in (-1, 0, 1, 2)}
            out.append(_Char(rx=rx, candidates=tuple(chr(o) for o in sorted(near)
                                                     if 0 < o <= 0x10FFFF and not 0xD800 <= o <= 0xDFFF)))
            i = end + 1
            continue
        else:
            out.append(_Char(literal=c, candidates=(c,)))
        i += 1
    return tuple(out)


def _common(x: _Char, y: _Char) -> str | None:
    for ch in (*x.candidates, *y.candidates, *_FALLBACK):
        if x.accepts(ch) and y.accepts(ch):
            return ch
    return None


def _segment_witness(a: str, b: str) -> str | None:
    """A non-empty segment both segment patterns match, or None."""
    return _segment_witness_cached(a, b) if a <= b else _segment_witness_cached(b, a)


@lru_cache(maxsize=4096)
def _segment_witness_cached(a: str, b: str) -> str | None:
    ta, tb = _tokens(a), _tokens(b)
    n, m = len(ta), len(tb)
    # w[i][j]: a string matched by both ta[i:] and tb[j:], or None. Filled from the ends, so no recursion.
    w: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    w[n][m] = ""
    for i in range(n, -1, -1):
        for j in range(m, -1, -1):
            if i == n and j == m:
                continue
            w[i][j] = _step(ta, tb, i, j, w)
    out = w[0][0]
    if out == "":
        return "a"  # only stars were used, and they match any non-empty segment too
    return out


def _step(ta: tuple[_Token, ...], tb: tuple[_Token, ...], i: int, j: int, w: list[list[str | None]]) -> str | None:
    n, m = len(ta), len(tb)
    if i < n and ta[i] is None:
        if w[i + 1][j] is not None:
            return w[i + 1][j]  # the star matches nothing more
        if j < m and (r := _absorb(tb[j], w[i][j + 1])) is not None:
            return r  # the star swallows what tb[j] matches
    if j < m and tb[j] is None:
        if w[i][j + 1] is not None:
            return w[i][j + 1]
        if i < n and (r := _absorb(ta[i], w[i + 1][j])) is not None:
            return r
    if i < n and j < m:
        x, y = ta[i], tb[j]
        if x is not None and y is not None and (rest := w[i + 1][j + 1]) is not None \
                and (ch := _common(x, y)) is not None:
            return ch + rest
    return None


def _absorb(token: _Token, rest: str | None) -> str | None:
    """What a star on the other side matches when it takes this token's character (or this whole star)."""
    if rest is None:
        return None
    if token is None:
        return rest
    ch = _common(token, _ANY)
    return None if ch is None else ch + rest


def _overlap_segments(g: Glob) -> tuple[str, ...]:
    """The segments with every GLOBSTAR meaning zero or more segments: a trailing one needs at least one segment,
    so it becomes '*' followed by one that may match none."""
    segs = g.segments
    if segs and segs[-1] == GLOBSTAR:
        segs = (*segs[:-1], "*", GLOBSTAR)
    return segs


def overlap(a: str, b: str) -> str | None:
    """A path both patterns match, or None when there is none. Raises ValueError or re.error on a bad pattern."""
    sa, sb = _overlap_segments(compile_glob(a)), _overlap_segments(compile_glob(b))
    n, m = len(sa), len(sb)
    w: list[list[list[str] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    w[n][m] = []
    for i in range(n, -1, -1):
        for j in range(m, -1, -1):
            if i == n and j == m:
                continue
            w[i][j] = _path_step(sa, sb, i, j, w)
    out = w[0][0]
    return "/".join(out) if out else None


def _path_step(sa: tuple[str, ...], sb: tuple[str, ...], i: int, j: int,
               w: list[list[list[str] | None]]) -> list[str] | None:
    n, m = len(sa), len(sb)
    if i < n and sa[i] == GLOBSTAR:
        if w[i + 1][j] is not None:
            return w[i + 1][j]  # ** spans no more segments
        if j < m and (rest := w[i][j + 1]) is not None:
            if sb[j] == GLOBSTAR:
                return rest
            if (s := _segment_witness("*", sb[j])) is not None:
                return [s, *rest]  # ** spans the segment sb[j] matches
    if j < m and sb[j] == GLOBSTAR:
        if w[i][j + 1] is not None:
            return w[i][j + 1]
        if i < n and (rest := w[i + 1][j]) is not None and (s := _segment_witness(sa[i], "*")) is not None:
            return [s, *rest]
    if i < n and j < m and sa[i] != GLOBSTAR and sb[j] != GLOBSTAR and (rest := w[i + 1][j + 1]) is not None \
            and (s := _segment_witness(sa[i], sb[j])) is not None:
        return [s, *rest]
    return None


_SPECIAL = re.compile(r"[*?\[\\]")


def literal(pattern: str) -> str | None:
    """The one path a pattern matches when it has no wildcard (an escaped file name), else None."""
    if not _SPECIAL.search(pattern):  # the common case, a plain file name: skip compiling (and caching) it
        return "/".join(s for s in normalise(pattern).split("/") if s) or None
    parts: list[str] = []
    for seg in compile_glob(pattern).segments:
        if seg == GLOBSTAR:
            return None
        chars = _tokens(seg)
        if any(t is None or t.literal is None for t in chars):
            return None
        parts.append("".join(t.literal for t in chars if t is not None and t.literal is not None))
    return "/".join(parts)


def invalid_patterns(patterns: object, max_length: int = 1000) -> list[str]:
    """Why a value is not a list of usable path globs (empty when it is)."""
    if not isinstance(patterns, list):
        return ["must be a list of path globs"]
    problems: list[str] = []
    for i, p in enumerate(patterns):
        if not isinstance(p, str) or not p.strip() or len(p) > max_length:
            problems.append(f"[{i}]: must be a non-empty string of at most {max_length} characters")
            continue
        try:
            compile_glob(p)
        except (ValueError, re.error) as e:
            problems.append(f"[{i}]: {p!r} is not a usable glob ({e})")
    return problems


def first_overlap(mine: Sequence[str], theirs: Sequence[str]) -> tuple[str, str, str] | None:
    """The first pair of patterns (one from each set) that can match a common path, with that path. A pattern that
    does not compile overlaps everything: an unreadable claim is not taken to be a free one."""
    lits_a, globs_a = _split(mine)
    lits_b, globs_b = _split(theirs)
    for path, p in lits_a.items():  # file vs file: equal paths
        if path in lits_b:
            return p, lits_b[path], path
    for path, p in lits_a.items():  # file vs glob: the glob matches the file
        for q in globs_b:
            if _safe_match(path, q):
                return p, q, path
    for path, q in lits_b.items():
        for p in globs_a:
            if _safe_match(path, p):
                return p, q, path
    for p in globs_a:  # glob vs glob
        for q in globs_b:
            try:
                hit = overlap(p, q)
            except (ValueError, re.error):
                return p, q, p
            if hit is not None:
                return p, q, hit
    return None


def _split(patterns: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    lits: dict[str, str] = {}
    rest: list[str] = []
    for p in patterns:
        try:
            path = literal(p)
        except (ValueError, re.error):
            path = None
        if path is None:
            rest.append(p)
        else:
            lits.setdefault(path, p)
    return lits, rest


def _safe_match(path: str, pattern: str) -> bool:
    try:
        return compile_glob(pattern).match(path)
    except (ValueError, re.error):
        return True
