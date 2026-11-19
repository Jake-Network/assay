"""Local configuration: .assay/config.toml (committable, no secrets) and environment variables."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_REL = Path(".assay") / "config.toml"


@dataclass
class LocalConfig:
    project: str | None = None
    default_budget_tokens: int = 2000
    path: Path | None = None


def find_config(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        p = d / CONFIG_REL
        if p.is_file():
            return p
    return None


def load(start: Path | None = None) -> LocalConfig:
    cfg = LocalConfig()
    p = find_config(start)
    if p is not None:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        cfg.project = data.get("project")
        cfg.default_budget_tokens = int(data.get("default_budget_tokens", 2000))
        cfg.path = p
    if env := os.environ.get("ASSAY_PROJECT"):
        cfg.project = env
    return cfg


def write_config(root: Path, project: str, budget: int = 2000) -> Path:
    p = root / CONFIG_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f'project = "{project}"\n'
        "# database_url is never stored here. Set ASSAY_DATABASE_URL for a shared Postgres;\n"
        "# without it assay uses the local embedded Postgres.\n"
        f"default_budget_tokens = {budget}\n",
        encoding="utf-8",
    )
    return p
