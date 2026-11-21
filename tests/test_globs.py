"""Guard globs: `**` across segments, everything else inside one segment, anchored at the repository root."""
from __future__ import annotations

import pytest

from benchbook.globs import GLOBSTAR, compile_glob, match, match_any, matching


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
