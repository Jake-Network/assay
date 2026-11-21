"""The source distribution ships what the repository tracks and nothing else."""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_sdist_only_include_matches_tracked_top_level() -> None:
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    top = {line.split("/", 1)[0] for line in tracked.splitlines() if line}
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    only = set(config["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"])
    assert only == top
