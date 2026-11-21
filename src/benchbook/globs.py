"""Path globs over repository-relative paths: guard paths now, path claims later.

A pattern is matched against the whole path, '/'-separated, from the repository root (no implicit "anywhere" as in
.gitignore). Within one path segment `*` matches any run of characters, `?` one character, `[abc]`/`[a-z]` one of a
set (`[!...]` or `[^...]` negates) and a backslash escapes the next character; none of them crosses a '/'. A
segment that is exactly `**` matches zero or more whole segments, or one or more at the end (`tests/**` is every file
under tests/, not a file named tests); `**` inside a segment (`a**b`) is two `*`.

Python 3.11 has neither glob.translate nor PurePath.full_match (3.13), and fnmatch lets `*` cross '/', so this module
does its own matching. A pattern is kept as its list of segments rather than one regular expression: deciding
whether two glob sets can match a common path (path claims) is then a walk over two segment lists that only has to
compare one segment pattern with another."""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache

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
