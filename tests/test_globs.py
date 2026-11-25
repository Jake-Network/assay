"""Path globs: `**` across segments, everything else inside one segment, anchored at the repository root; and the
overlap of two glob sets that path occupancy decides on."""
from __future__ import annotations

import functools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from benchbook.checks.globs import (
    GLOBSTAR,
    compile_glob,
    escape,
    first_overlap,
    invalid_patterns,
    literal,
    match,
    match_any,
    matching,
    overlap,
)


@pytest.mark.parametrize(("pattern", "path", "expected"), [
    ("tests/**", "tests/a.py", True),
    ("tests/**", "tests/a/b/c.py", True),
    ("tests/**", "tests", False),  # a trailing ** needs at least one segment
    ("tests/**", "src/tests/a.py", False),  # anchored at the root
    ("**/conftest.py", "conftest.py", True),  # a leading ** may match nothing
    ("**/conftest.py", "a/b/conftest.py", True),
    ("**/conftest.py", "a/b/my_conftest.py", False),
    (".benchbook/config.toml", ".benchbook/config.toml", True),
    (".benchbook/config.toml", "x/.benchbook/config.toml", False),
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/a/b.py", False),  # * stays inside a segment
    ("a/**/b", "a/b", True),
    ("a/**/b", "a/x/y/b", True),
    ("a/**/b", "a/x/y/c", False),
    ("a**b", "axyb", True),  # ** inside a segment is two *
    ("a**b", "ax/yb", False),
    ("t?st.py", "test.py", True),
    ("t?st.py", "t/st.py", False),
    ("[tb]est", "best", True),
    ("[!tb]est", "best", False),
    ("[^tb]est", "rest", True),
    ("[a-c]x", "bx", True),
    ("[a", "[a", True),  # an unclosed class is a literal [
    ("a\\*", "a*", True),  # backslash escapes
    ("a\\*", "ab", False),
    ("**", "any/path.py", True),
    ("./tests/**", "tests/x", True),
    ("src/**/test_*.py", "src/test_x.py", True),
    ("src/**/test_*.py", "src/m/n/test_x.py", True),
    ("src/**/test_*.py", "src/m/n/x_test.py", False),
])
def test_match(pattern: str, path: str, expected: bool) -> None:
    assert match(path, pattern) is expected


def test_segments_are_kept_for_overlap_checks() -> None:
    """Path claims will compare two glob sets segment by segment, so a pattern keeps its segment list."""
    g = compile_glob("a/**/**/b//c*")
    assert g.segments == ("a", GLOBSTAR, "b", "c*")
    assert compile_glob("a/**/**/b//c*") is g  # compiled once


def test_helpers() -> None:
    assert match_any("tests/x.py", ["src/**", "tests/**"])
    assert not match_any("README.md", ["src/**", "tests/**"])
    paths = ["src/a.py", "tests/t.py", "conftest.py", "docs/x.md"]
    assert matching(paths, ["tests/**", "**/conftest.py"]) == ["tests/t.py", "conftest.py"]
    with pytest.raises(ValueError):
        compile_glob("/")


# ------------------------------------------------------------------------------------------- overlap (path occupancy)

@pytest.mark.parametrize(("a", "b", "common"), [
    ("src/a.py", "src/a.py", True),  # file vs file
    ("src/a.py", "src/b.py", False),
    ("src/a.py", "src/*.py", True),  # file vs glob
    ("src/a/b.py", "src/*.py", False),  # * stays in its segment
    ("src/new/**", "src/*/x.py", True),  # glob vs glob, neither matching a file yet
    ("src/new/**", "src/other/*.py", False),
    ("tests/**", "tests", False),  # a trailing ** needs a segment
    ("**", "README.md", True),
    ("a/**/b", "a/b", True),
    ("a/*/c", "a/**/c", True),
    ("x*y", "*z*", True),
    ("a/[ab]x", "a/[!ab]x", False),
    ("[a-c]", "[!a-b]", True),
    ("a\\*", "a?", True),  # the escaped star is the character '*'
    ("a\\*", "ab", False),
])
def test_overlap(a: str, b: str, common: bool) -> None:
    for x, y in ((a, b), (b, a)):
        w = overlap(x, y)
        assert (w is not None) is common
        if w is not None:  # the answer is a path both match
            assert match(w, x) and match(w, y)


def test_escape_keeps_a_file_one_file() -> None:
    """Next.js routes and the like put [ ] in file names; escaped, the file matches only itself."""
    p = escape("app/[id]/page.tsx")
    assert literal(p) == "app/[id]/page.tsx" and match("app/[id]/page.tsx", p) and not match("app/i/page.tsx", p)
    assert literal("src/*.py") is None and literal("./src//a.py") == "src/a.py"
    assert first_overlap([p], ["app/*/page.tsx"]) == (p, "app/*/page.tsx", "app/[id]/page.tsx")
    assert first_overlap([p], ["app/[x]/page.tsx"]) is None


def test_first_overlap_names_the_pair() -> None:
    assert first_overlap(["src/a.py", "docs/x.md"], ["lib/**", "docs/x.md"]) == ("docs/x.md", "docs/x.md", "docs/x.md")
    assert first_overlap(["src/new/**"], ["src/*/x.py"]) == ("src/new/**", "src/*/x.py", "src/new/x.py")
    assert first_overlap(["src/a.py"], ["src/b.py", "lib/**"]) is None
    assert first_overlap([], ["**"]) is None
    assert first_overlap(["x/[z-a]"], ["y"]) == ("x/[z-a]", "y", "y")  # an unreadable glob is taken to overlap
    assert first_overlap(["x/[z-a]"], ["y/*"]) == ("x/[z-a]", "y/*", "x/[z-a]")
    assert invalid_patterns(["ok/**", "", "x/[z-a]"]) == [
        "[1]: must be a non-empty string of at most 1000 characters",
        "[2]: 'x/[z-a]' is not a usable glob (bad character range z-a at position 1)"]
    assert invalid_patterns("src/**") == ["must be a list of path globs"]


# Property test against brute force: every path of up to three segments of one or two characters from a, b, c.
_ALPHABET = "abc"
_SEGS = [x + y for x in _ALPHABET for y in ("", *_ALPHABET)]
_UNIVERSE = [*(_SEGS), *(f"{a}/{b}" for a in _SEGS for b in _SEGS),
             *(f"{a}/{b}/{c}" for a in _SEGS for b in _SEGS for c in _SEGS)]
_TOKENS = st.sampled_from(["a", "b", "c", "*", "?", "[ab]", "[!a]", "[b-c]", "[^bc]"])
_SEGMENT = st.one_of(st.just(GLOBSTAR), st.lists(_TOKENS, min_size=1, max_size=3).map("".join))
_PATTERN = st.lists(_SEGMENT, min_size=1, max_size=3).map("/".join)


@functools.cache
def _matches(pattern: str) -> frozenset[str]:
    return frozenset(p for p in _UNIVERSE if match(p, pattern))


@settings(max_examples=400, deadline=None)
@given(_PATTERN, _PATTERN)
def test_overlap_agrees_with_brute_force(a: str, b: str) -> None:
    w = overlap(a, b)
    if _matches(a) & _matches(b):
        assert w is not None, "a common path exists, so the patterns overlap"
    if w is not None:
        assert match(w, a) and match(w, b), f"{w!r} is not matched by both"
    assert (overlap(b, a) is None) == (w is None)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.one_of(_PATTERN, st.sampled_from(_UNIVERSE).map(escape)), max_size=4),
       st.lists(st.one_of(_PATTERN, st.sampled_from(_UNIVERSE).map(escape)), max_size=4))
def test_set_overlap_agrees_with_brute_force(a: list[str], b: list[str]) -> None:
    common = set().union(*map(_matches, a)) & set().union(*map(_matches, b)) if a and b else set()
    hit = first_overlap(a, b)
    if common:
        assert hit is not None
    if hit is not None:
        p, q, w = hit
        assert p in a and q in b and match(w, p) and match(w, q)
