"""Local configuration: .benchbook/config.toml (committable, no secrets) and environment variables.

The project was called assay before v0.1.0. This module is where that name is still read: ASSAY_* environment
variables, a .assay/ config folder and the state directory under the old name keep working during 0.1.x, so an
install from before the rename does not lose its project, its database target or its queued hook reports.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

NAME = "benchbook"
LEGACY_NAME = "assay"
CONFIG_DIR = Path("." + NAME)
LEGACY_CONFIG_DIR = Path("." + LEGACY_NAME)
CONFIG_FILE = "config.toml"
CONFIG_REL = CONFIG_DIR / CONFIG_FILE
LEGACY_CONFIG_REL = LEGACY_CONFIG_DIR / CONFIG_FILE
ENV_PREFIX = "BENCHBOOK_"
LEGACY_ENV_PREFIX = "ASSAY_"

# ---------------------------------------------------------------------- notices about the old name

# Set by the `assay` console script (entry.legacy_main). The process was started by something that still uses the
# old names, so what it is shown by name (the MCP tools) keeps them too.
LEGACY_COMMAND = False

_told: set[str] = set()
_held: list[str] | None = None


def legacy_notice(key: str, message: str) -> None:
    """Say once per process (per key) that something was read under the old name. It goes to stderr, except after
    hold_notices(): a Claude Code hook must never write there, so its notes wait for the hook log."""
    if key in _told:
        return
    _told.add(key)
    if _held is None:
        print(f"{NAME}: {message}", file=sys.stderr)
    else:
        _held.append(message)


def hold_notices() -> None:
    """Keep legacy notices off stderr for the rest of this process; take_notices() hands them over."""
    global _held
    if _held is None:
        _held = []


def take_notices() -> list[str]:
    """The notices held so far (each is returned once)."""
    if not _held:
        return []
    out = list(_held)
    _held.clear()
    return out


# ---------------------------------------------------------------------- environment


def env_name(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The variable that supplies <name>: BENCHBOOK_<name>, else the pre-rename ASSAY_<name>, else None."""
    source = os.environ if environ is None else environ
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        if source.get(prefix + name):
            return prefix + name
    return None


def env(name: str, default: str | None = None, environ: Mapping[str, str] | None = None) -> str | None:
    """The value of BENCHBOOK_<name>, or of the pre-rename ASSAY_<name> when only that one is set (the user is told
    once per process). An empty value counts as unset. environ: read this mapping instead of os.environ."""
    used = env_name(name, environ)
    if used is None:
        return default
    if used.startswith(LEGACY_ENV_PREFIX):
        # One line for the process, naming every old variable in the environment, not only the first one read.
        names = sorted({used, *legacy_variables()})
        legacy_notice("env", f"{', '.join(names)}: {'this is the name' if len(names) == 1 else 'these are the names'} "
                             f"from before the rename to {NAME}, still read in 0.1.x; rename to "
                             f"{', '.join(ENV_PREFIX + n[len(LEGACY_ENV_PREFIX):] for n in names)}")
    return (os.environ if environ is None else environ)[used]


def legacy_variables(environ: Mapping[str, str] | None = None) -> list[str]:
    """The ASSAY_* variables that are set while their BENCHBOOK_* name is not, so the old name is what is read."""
    source = os.environ if environ is None else environ
    return sorted(k for k, v in source.items() if k.startswith(LEGACY_ENV_PREFIX) and v
                  and not source.get(ENV_PREFIX + k[len(LEGACY_ENV_PREFIX):]))


# ---------------------------------------------------------------------- directories


def prefer_legacy(new: Path, old: Path, marker: str | None = None) -> bool:
    """Whether to keep using a location from before the rename: it exists and the new one does not. Nothing is ever
    moved for the user (a server or a hook may be using the folder); `benchbook doctor` prints how to move it.
    marker: a file that must be inside for a location to count as existing (an empty folder holds no data)."""
    def present(p: Path) -> bool:
        return (p / marker).exists() if marker else p.is_dir()

    return present(old) and not present(new)


def default_state_dirs() -> tuple[Path, Path]:
    """(the state directory, the one from before the rename)."""
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / NAME, base / LEGACY_NAME


def state_dir() -> Path:
    """Where the hook log, spool and handback markers live. An existing pre-rename directory stays in use while
    there is no new one, so reports queued before the upgrade are still replayed."""
    new, old = default_state_dirs()
    return old if prefer_legacy(new, old) else new


def local_dir(root: Path) -> Path:
    """The folder under root that holds benchbook's files for a checkout: .benchbook, or a pre-rename .assay while
    it is the only one there."""
    new, old = root / CONFIG_DIR, root / LEGACY_CONFIG_DIR
    return old if prefer_legacy(new, old) else new


# ---------------------------------------------------------------------- config.toml


def default_agent(name: str) -> str | None:
    """The fixed name an adapter falls back to (the hook's 'hook', the viewer's 'human'), unless BENCHBOOK_TOKEN is
    set: a token already says which agent this process is, and Ledger.open refuses any other name."""
    return None if env("TOKEN") else name


@dataclass
class LocalConfig:
    project: str | None = None
    default_budget_tokens: int = 2000
    path: Path | None = None
    legacy: bool = False  # read from .assay/config.toml (the folder's name before the rename)


def _config_in(d: Path) -> Path | None:
    for rel in (CONFIG_REL, LEGACY_CONFIG_REL):  # .benchbook wins at the same level
        if (d / rel).is_file():
            return d / rel
    return None


def find_config(start: Path | None = None) -> Path | None:
    """.benchbook/config.toml (or a pre-rename .assay/config.toml) in the start directory or a parent; in a linked
    git worktree where the folder is not committed, the main worktree's copy (U6)."""
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (p := _config_in(d)) is not None:
            return p
    main = _main_worktree(cur)
    return _config_in(main) if main is not None else None


def _main_worktree(cur: Path) -> Path | None:
    """The main worktree of the linked git worktree containing cur. Reads the .git file and commondir instead of
    running git, so hooks stay fast."""
    for d in (cur, *cur.parents):
        dot = d / ".git"
        if dot.is_dir():
            return None  # the main worktree itself (or a plain repository)
        if not dot.is_file():
            continue
        try:
            line = dot.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = (d / line[len("gitdir:"):].strip()).resolve()
            common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        except OSError:
            return None
        return common.parent if common.name == ".git" else None
    return None


def load(start: Path | None = None) -> LocalConfig:
    cfg = LocalConfig()
    p = find_config(start)
    if p is not None:
        import tomllib  # here, not at the top: the hook entry imports this module for every subagent stop

        data = tomllib.loads(p.read_text(encoding="utf-8"))
        cfg.project = data.get("project")
        cfg.default_budget_tokens = int(data.get("default_budget_tokens", 2000))
        cfg.path = p
        cfg.legacy = p.parent.name == LEGACY_CONFIG_DIR.name
    if project := env("PROJECT"):
        cfg.project = project
    return cfg


def write_config(root: Path, project: str, budget: int = 2000) -> Path:
    """Write config.toml into root's benchbook folder. A checkout that still has only .assay/ keeps it: its other
    files (autoresearch state, queued runs) live there, and `git mv .assay .benchbook` moves them together."""
    p = local_dir(root) / CONFIG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f'project = "{project}"\n'
        "# database_url is never stored here. Set BENCHBOOK_DATABASE_URL for a shared Postgres;\n"
        "# without it benchbook uses the local embedded Postgres.\n"
        f"default_budget_tokens = {budget}\n",
        encoding="utf-8",
    )
    return p
