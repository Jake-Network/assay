"""The checks `benchbook init` proposes for a code project (T07): the test and lint commands a repository already
declares in pyproject.toml, package.json, a Makefile, go.mod or Cargo.toml, with the guard paths of those languages
and the base branch. Standard library only; nothing is run."""
from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NPM_DEFAULT_TEST = "no test specified"


@dataclass
class Proposal:
    checks: dict[str, str] = field(default_factory=dict)  # name -> cmd (a pytest cmd carries --junitxml={junit})
    guard_paths: list[str] = field(default_factory=list)
    base: str | None = None
    found_in: list[str] = field(default_factory=list)

    def add(self, name: str, cmd: str, source: str) -> None:
        if name not in self.checks:
            self.checks[name] = cmd
            if source not in self.found_in:
                self.found_in.append(source)

    def guard(self, *paths: str) -> None:
        self.guard_paths += [p for p in paths if p not in self.guard_paths]

    def toml(self) -> str:
        out = []
        for name, cmd in self.checks.items():
            out += [f"[checks.{name}]", f"cmd = {json.dumps(cmd, ensure_ascii=False)}"]
            if "{junit}" in cmd:
                out.append("junit = true")
            out.append("")
        out += ["[guard]", f"paths = {json.dumps([*self.guard_paths, '.benchbook/config.toml'])}"]
        if self.base:
            out.append(f"base = {json.dumps(self.base)}")
        return "\n".join(out) + "\n"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _python(root: Path, prop: Proposal) -> None:
    text = _read(root / "pyproject.toml")
    if not text:
        return
    try:
        data: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    run = "uv run " if (root / "uv.lock").is_file() else "poetry run " if (root / "poetry.lock").is_file() else ""
    has_tests = (root / "tests").is_dir() or any(root.glob("test_*.py"))
    if "pytest" in text or ("pytest" in data.get("tool", {})) or has_tests:
        prop.add("unit", f"{run or 'python -m '}pytest -q --junitxml={{junit}}", "pyproject.toml")
        prop.guard("tests/**", "**/conftest.py", "**/test_*.py")
    if "ruff" in text:
        prop.add("lint", f"{run}ruff check", "pyproject.toml")


def _node(root: Path, prop: Proposal) -> None:
    try:
        scripts = json.loads(_read(root / "package.json") or "{}").get("scripts") or {}
    except ValueError:
        return
    if not isinstance(scripts, dict):
        return
    pm = ("pnpm" if (root / "pnpm-lock.yaml").is_file() else "yarn" if (root / "yarn.lock").is_file()
          else "bun" if (root / "bun.lockb").is_file() else "npm")
    test = scripts.get("test")
    if isinstance(test, str) and test.strip() and NPM_DEFAULT_TEST not in test:
        prop.add("unit", f"{pm} test", "package.json")
        prop.guard("**/*.test.*", "**/*.spec.*", "**/__tests__/**")
    if isinstance(scripts.get("lint"), str):
        prop.add("lint", f"{pm} run lint", "package.json")


def _make(root: Path, prop: Proposal) -> None:
    text = _read(root / "Makefile")
    for target, name in (("test", "unit"), ("lint", "lint")):
        if re.search(rf"(?m)^{target}\s*:", text):
            prop.add(name, f"make {target}", "Makefile")


def _go_rust(root: Path, prop: Proposal) -> None:
    if (root / "go.mod").is_file():
        prop.add("unit", "go test ./...", "go.mod")
        prop.add("lint", "go vet ./...", "go.mod")
        prop.guard("**/*_test.go")
    if (root / "Cargo.toml").is_file():
        prop.add("unit", "cargo test", "Cargo.toml")
        prop.guard("tests/**")


def base_branch(root: Path) -> str | None:
    """origin's default branch (origin/main), else a local main or master."""
    def git(*args: str) -> str:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ""

    if head := git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
        return head
    for name in ("main", "master"):
        if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return None


def propose(root: Path) -> Proposal:
    prop = Proposal()
    for find in (_python, _node, _make, _go_rust):
        find(root, prop)
    prop.base = base_branch(root)
    return prop
