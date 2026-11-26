"""Integrity of a commit against its merge-base: the guarded paths it changed, whether a change only adds tests or
weakens them (T04), and whether the [checks] declarations moved."""
from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.globs import matching
from .declarations import Base
from .gitrepo import _diff_u0, _git, _Unreadable, changed_paths, config_at

MAX_GUARD_FILES = 100  # stored in metrics; guard_touched is computed from all of them
# T04: an added line with one of these makes a guarded file weakened even when nothing was deleted.
SKIP_MARKERS = ("pytest.mark.skip", "pytest.mark.xfail", "pytest.skip(", "unittest.skip", "it.skip(", "describe.skip(",
                "test.skip(", "xit(", "t.Skip(", "#[ignore]", "@Disabled", "@Ignore")
# Files whose any change can weaken every test (an autouse fixture, a collection hook): never "added only".
ALWAYS_WEAKENING = ("conftest.py",)


@dataclass
class Integrity:
    guard_files: list[str]  # changed files (deleted ones included) that match a guard glob
    checks_changed: bool
    # T04: guard_files split by what the change did. added: new files; appended: lines added, none deleted or
    # changed; weakened: anything else, a skip marker added, or any change to a conftest.py.
    guard_added: list[str] = field(default_factory=list)
    guard_appended: list[str] = field(default_factory=list)
    guard_weakened_files: list[str] = field(default_factory=list)
    tests_before: int | None = None  # JUnit testcases that ran at the merge-base, over the JUnit checks
    tests_after: int | None = None
    # The JUnit checks that ran fewer testcases than at the merge-base, each compared with itself: tests added to one
    # check never make up for tests gone from another.
    fewer_tests_in: list[str] = field(default_factory=list)

    @property
    def guard_touched(self) -> bool:
        return bool(self.guard_files)

    @property
    def fewer_tests(self) -> bool:
        return bool(self.fewer_tests_in)

    @property
    def guard_weakened(self) -> bool:
        """What sends a commit to a person (T04): a guarded file weakened, or a JUnit check ran fewer tests than at
        the merge-base. Tests only added are not."""
        return bool(self.guard_weakened_files) or self.fewer_tests


def integrity(repo: Path, base: Base, commit: str, pin_moved: bool = False) -> Integrity:
    """Step 2. pin_moved: the pinned checks changed under the work (pin_changed), which counts as a [checks] change."""
    guard_files = matching(changed_paths(repo, base.merge_base, commit), base.guard_paths)
    added, appended, weakened = classify_guard(repo, base.merge_base, commit, guard_files)
    try:
        found = config_at(repo, commit)
    except _Unreadable:
        return Integrity(guard_files, True, added, appended, weakened)
    before = config_at(repo, base.merge_base)
    old = before[0].get("checks") if before else None
    new = found[0].get("checks") if found else None
    # parsed tables: comments and layout are no change
    return Integrity(guard_files, old != new or pin_moved, added, appended, weakened)


def classify_guard(repo: Path, merge_base: str, commit: str,
                   files: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """T04: (added, appended, weakened) of the changed guarded files. A new file is added and a file with lines added
    and none deleted is appended, unless an added line carries a SKIP_MARKERS entry or the file is a conftest.py;
    everything else (deleted, rewritten, binary) is weakened. A Python file whose deleted lines are all in import
    statements can also count as lines added when the import bindings are preserved (_imports_extended)."""
    if not files:
        return [], [], []
    out = _git(repo, "diff", "--numstat", "--no-renames", "-z", merge_base, commit, "--", *files).stdout
    deleted: dict[str, int | None] = {}
    for entry in out.split("\0"):
        parts = entry.split("\t")
        if len(parts) == 3 and parts[2]:
            deleted[parts[2]] = int(parts[1]) if parts[1].isdigit() else None  # "-": binary
    status = {}
    names = _git(repo, "diff", "--name-status", "--no-renames", "-z", merge_base, commit, "--", *files).stdout
    items = [x for x in names.split("\0") if x]
    for st, path in zip(items[::2], items[1::2], strict=False):
        status[path] = st[:1]
    added: list[str] = []
    appended: list[str] = []
    weakened: list[str] = []
    for f in files:
        st, dels = status.get(f, ""), deleted.get(f)
        if Path(f).name in ALWAYS_WEAKENING or st not in ("A", "M") or dels is None:
            weakened.append(f)
        elif _adds_skip(diff := _diff_u0(repo, merge_base, commit, f)):
            weakened.append(f)
        elif st == "M" and dels > 0 and not _imports_extended(repo, merge_base, commit, f, diff):
            weakened.append(f)
        else:
            (added if st == "A" else appended).append(f)
    return added, appended, weakened


def _adds_skip(diff: str) -> bool:
    return any(line.startswith("+") and not line.startswith("+++") and any(m in line for m in SKIP_MARKERS)
               for line in diff.splitlines())


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)


def _imports_extended(repo: Path, merge_base: str, commit: str, path: str, diff: str) -> bool:
    """Allow a Python test to extend module-level imports without changing existing bindings or statement order."""
    if not path.endswith(".py"):
        return False
    deleted = {start + i for m in HUNK.finditer(diff)
               for start, n in [(int(m.group(1)), 1 if m.group(2) is None else int(m.group(2)))] for i in range(n)}
    trees = []
    for ref in (merge_base, commit):
        shown = _git(repo, "show", f"{ref}:{path}", check=False)
        if shown.returncode != 0:
            return False
        try:
            trees.append(ast.parse(shown.stdout))
        except (SyntaxError, ValueError):
            return False
    old, new = trees
    top_level = {node for node in old.body if isinstance(node, (ast.Import, ast.ImportFrom))}
    in_import: set[int] = set()
    elsewhere: set[int] = set()
    for node in ast.walk(old):
        if node in top_level and isinstance(node, (ast.Import, ast.ImportFrom)):
            in_import.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        elif isinstance(node, ast.stmt):
            # A compound statement holds its body's lines: only its header line is its own.
            own = node.lineno if hasattr(node, "body") else None
            elsewhere.update([own] if own is not None else range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return bool(deleted and deleted <= in_import and not deleted & elsewhere and _keeps_import_bindings(old, new))


def _bound_name(alias: ast.alias) -> str:
    return alias.asname or alias.name.split(".", 1)[0]


def _added_imports(old: ast.stmt | None, new: ast.Import | ast.ImportFrom) -> list[ast.alias] | None:
    """New names in the same import statement when its old names are still in order."""
    if not isinstance(old, (ast.Import, ast.ImportFrom)) or type(old) is not type(new):
        return None
    if isinstance(old, ast.ImportFrom) and isinstance(new, ast.ImportFrom):
        if (old.level, old.module) != (new.level, new.module):
            return None
    added = []
    remaining = iter(old.names)
    expected = next(remaining, None)
    for alias in new.names:
        if expected is not None and (alias.name, alias.asname) == (expected.name, expected.asname):
            expected = next(remaining, None)
        else:
            added.append(alias)
    return added if expected is None else None


def _keeps_import_bindings(old: ast.Module, new: ast.Module) -> bool:
    # References matter as much as declarations: a new import can also shadow a builtin used in an assertion.
    names: set[str] = set()
    for node in ast.walk(old):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(_bound_name(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)

    remaining = iter(old.body)
    expected = next(remaining, None)
    for node in new.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            added = _added_imports(expected, node)
            if added is None:
                added = node.names
            else:
                expected = next(remaining, None)
            if added and isinstance(node, ast.ImportFrom) and node.module == "__future__":
                return False
            for alias in added:
                name = _bound_name(alias)
                if name == "*" or name in names:
                    return False
                names.add(name)
        elif expected is not None and ast.dump(node) == ast.dump(expected):
            expected = next(remaining, None)
    return expected is None
