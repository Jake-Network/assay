"""`assay doctor` checks for the Claude Code connection. Each failing check carries the one command that fixes it.

Claude Code starts `assay mcp` and `assay hook` itself, with its own PATH and the `env` of its settings files. When
those differ from the shell the user tests in, the MCP server does not connect or the hook exits 127 at every
subagent stop, and Claude Code does not say why. These checks compare the two sides.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN = "assay@assay"
ENV_KEYS = ("ASSAY_DATA_DIR", "ASSAY_PROJECT")
RECENT_SECONDS = 24 * 3600
# hook.log lines that mean a report did not reach the ledger (see hooks.py)
FAILURE_MARKERS = ("failed", "not captured", "moved to", "quarantined", "cannot read")
NO_PROJECT = re.compile(r"no assay project for (.+?)(?: \(|$)")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _read_json(p: Path) -> dict[str, Any]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def settings_files(root: Path) -> list[Path]:
    """Claude Code settings that can carry `env` and `enabledPlugins`, lowest precedence first."""
    return [claude_dir() / "settings.json", root / ".claude" / "settings.json",
            root / ".claude" / "settings.local.json"]


def _effective(root: Path, section: str, key: str) -> tuple[Any, Path | None]:
    """The value Claude Code uses for settings[section][key] and the file it comes from."""
    value: Any = None
    source: Path | None = None
    for f in settings_files(root):
        sec = _read_json(f).get(section)
        if isinstance(sec, dict) and key in sec:
            value, source = sec[key], f
    return value, source


def _assay_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "version unknown"
    return out.stdout.strip() or "version unknown"


def check_path(root: Path) -> Check:
    env_path, source = _effective(root, "env", "PATH")
    path = str(env_path) if env_path else os.environ.get("PATH", "")
    where = f"PATH from {source}" if source else "PATH"
    exe = shutil.which("assay", path=path)
    if exe:
        return Check("assay on PATH", True, f"{exe} ({_assay_version(exe)})")
    return Check("assay on PATH", False,
                 f"`assay` is not on the {where} Claude Code starts `assay mcp` and `assay hook` from",
                 "uv tool update-shell, then open a new terminal and restart Claude Code (if `uv tool list` has no "
                 "assay: uv tool install --from git+https://github.com/Jake-Network/assay assay)")


def _manual_install(root: Path) -> bool:
    servers = _read_json(root / ".mcp.json").get("mcpServers")
    return isinstance(servers, dict) and "assay" in servers


def check_plugin(root: Path) -> Check:
    installed = _read_json(claude_dir() / "plugins" / "installed_plugins.json")
    plugins = installed.get("plugins", installed)
    has = isinstance(plugins, dict) and PLUGIN in plugins
    enabled, _ = _effective(root, "enabledPlugins", PLUGIN)
    if has and enabled is True:
        return Check("claude code plugin", True, f"{PLUGIN} installed and enabled")
    if _manual_install(root):
        return Check("claude code plugin", True, "manual install (.mcp.json has an assay server)")
    if has:
        return Check("claude code plugin", False, f"{PLUGIN} is installed but not enabled",
                     f"claude plugin enable {PLUGIN}")
    return Check("claude code plugin", False, f"{PLUGIN} is not installed",
                 f"claude plugin marketplace add Jake-Network/assay && claude plugin install {PLUGIN}")


def _norm(key: str, v: str | None) -> str | None:
    if not v:
        return None
    if key == "ASSAY_DATA_DIR":
        return str(Path(v).expanduser().resolve())
    return v.strip()


def check_env(root: Path) -> list[Check]:
    out: list[Check] = []
    for key in ENV_KEYS:
        raw, source = _effective(root, "env", key)
        claude = None if raw is None else str(raw)
        shell = os.environ.get(key)
        if _norm(key, claude) == _norm(key, shell):
            out.append(Check(key, True, f"{shell or 'unset'} (shell and Claude Code)"))
            continue
        target = source or settings_files(root)[0]
        detail = f"shell {shell or '(unset)'}, Claude Code {claude or '(unset)'}" + (f" ({source})" if source else "")
        fix = (f'set "env": {{"{key}": "{shell}"}} in {target}, then restart Claude Code' if shell
               else f"export {key}={claude}   (add it to your shell profile)")
        out.append(Check(key, False, detail, fix))
    return out


def _parse_ts(line: str) -> float | None:
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _counts(msg: str, root: Path) -> bool:
    """A "no assay project" line from another repository is expected when the plugin is installed for the user and
    Claude Code runs elsewhere; only one from this repository means a report was lost here."""
    if not any(m in msg for m in FAILURE_MARKERS):
        return False
    if m := NO_PROJECT.search(msg):
        where = Path(m.group(1))
        return where == root or root in where.parents
    return True


def check_hook_log(root: Path) -> Check:
    """Failure lines in hook.log since the later of 24 hours ago and the last clean `assay hook replay`, and the
    reports waiting in the spool's dead/ folder for this database."""
    from .hooks import _spool_dir, _state_dir

    log = _state_dir() / "hook.log"
    since = time.time() - RECENT_SECONDS
    failures: list[str] = []
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        ts = _parse_ts(line)
        if ts is None or ts < since:
            continue
        msg = line[20:]
        if msg.startswith("replay:") and "dead 0; still queued 0" in msg:
            failures = []  # everything before a clean replay was dealt with
        elif _counts(msg, root):
            failures.append(line)
    dead = _spool_dir() / "dead"
    n_dead = len(list(dead.glob("*.json"))) if dead.is_dir() else 0
    if not failures and not n_dead:
        return Check("hook failures", True, f"none in the last 24h; dead/ empty ({log})")
    detail = f"{len(failures)} failure line(s) in the last 24h, {n_dead} report(s) in {dead}"
    if failures:
        detail += f"\n      last: {failures[-1][:200]}"
    return Check("hook failures", False, detail, f"read {log}, fix the cause, then run: assay hook replay")


def claude_code_checks(root: Path) -> list[Check]:
    checks: list[Check] = []
    if claude_dir().is_dir():
        checks += [check_path(root), check_plugin(root), *check_env(root)]
    else:
        checks.append(Check("claude code", True, f"{claude_dir()} not found; connection checks skipped"))
    checks.append(check_hook_log(root.resolve()))
    return checks
