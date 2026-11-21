"""`benchbook doctor` checks for the Claude Code connection. Each failing check carries the one command that fixes it.

Claude Code starts `benchbook mcp` and `benchbook hook` itself, with its own PATH and the `env` of its settings
files. When those differ from the shell the user tests in, the MCP server does not connect or the hook exits 127 at
every subagent stop, and Claude Code does not say why. These checks compare the two sides.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN = "benchbook@benchbook"
ENV_KEYS = ("BENCHBOOK_DATABASE_URL", "BENCHBOOK_DATA_DIR", "BENCHBOOK_PROJECT")
# values doctor compares but never prints: a database URL can carry a password
SECRET_KEYS = {"BENCHBOOK_DATABASE_URL"}
RECENT_SECONDS = 24 * 3600
# hook.log lines that mean a report did not reach the ledger (see hooks.py)
FAILURE_MARKERS = ("failed", "not captured", "moved to", "quarantined", "cannot read")
NO_PROJECT = re.compile(r"no benchbook project for (.+?)(?: \(|$)")
SPOOL_KEY = re.compile(r"/spool/([0-9a-f]{64})\b")
# a queued report older than this was not drained by the background processor (its retries take about a minute)
STUCK_SECONDS = 300


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


def check_path(root: Path) -> Check:
    env_path, source = _effective(root, "env", "PATH")
    path = str(env_path) if env_path else os.environ.get("PATH", "")
    where = f"PATH from {source}" if source else "PATH"
    exe = shutil.which("benchbook", path=path)
    if exe:
        # Only the path: the PATH can come from a repository's settings file, so the binary it finds is not
        # trusted enough to run. `benchbook build` above is the version of the benchbook running this check.
        return Check("benchbook on PATH", True, exe)
    return Check("benchbook on PATH", False,
                 f"`benchbook` is not on the {where} Claude Code starts `benchbook mcp` and `benchbook hook` from",
                 "uv tool update-shell, then open a new terminal and restart Claude Code (if `uv tool list` has no "
                 "benchbook: uv tool install --from git+https://github.com/Jake-Network/benchbook benchbook)")


def _manual_install(root: Path) -> bool:
    servers = _read_json(root / ".mcp.json").get("mcpServers")
    return isinstance(servers, dict) and "benchbook" in servers


def check_plugin(root: Path) -> Check:
    installed = _read_json(claude_dir() / "plugins" / "installed_plugins.json")
    plugins = installed.get("plugins", installed)
    has = isinstance(plugins, dict) and PLUGIN in plugins
    enabled, _ = _effective(root, "enabledPlugins", PLUGIN)
    if has and enabled is True:
        return Check("claude code plugin", True, f"{PLUGIN} installed and enabled")
    if _manual_install(root):
        return Check("claude code plugin", True, "manual install (.mcp.json has an benchbook server)")
    if has:
        return Check("claude code plugin", False, f"{PLUGIN} is installed but not enabled",
                     f"claude plugin enable {PLUGIN}")
    return Check("claude code plugin", False, f"{PLUGIN} is not installed",
                 f"claude plugin marketplace add Jake-Network/benchbook && claude plugin install {PLUGIN}")


def _norm(key: str, v: str | None) -> Any:
    """The value as the database target it selects, so that equal targets written differently compare equal.
    Raises ValueError for a value that names no target."""
    if not v:
        return None
    if key == "BENCHBOOK_DATA_DIR":
        try:
            return str(Path(v).expanduser().resolve())
        except RuntimeError as e:  # ~name of a user that does not exist
            raise ValueError(f"cannot expand {v!r}: {e}") from e
    if key == "BENCHBOOK_DATABASE_URL":
        from psycopg.conninfo import conninfo_to_dict

        try:
            parts = conninfo_to_dict(v.strip())
        except Exception as e:  # noqa: BLE001 (never echo the value: it can carry a password)
            raise ValueError("not a valid connection string") from e
        # the server and database it reaches; the password does not change the target
        return sorted((k, str(val)) for k, val in parts.items() if k != "password")
    return v.strip()


def _shown(key: str, v: str | None) -> str:
    if not v:
        return "(unset)"
    return "(set, not shown)" if key in SECRET_KEYS else v


def check_env(root: Path) -> list[Check]:
    out: list[Check] = []
    url_claude, _ = _effective(root, "env", "BENCHBOOK_DATABASE_URL")
    url_shell = os.environ.get("BENCHBOOK_DATABASE_URL")
    for key in ENV_KEYS:
        raw, source = _effective(root, "env", key)
        claude = None if raw is None else str(raw)
        shell = os.environ.get(key)
        target = source or settings_files(root)[0]
        where = f" ({source})" if source else ""
        try:
            n_shell = _norm(key, shell)
        except ValueError as e:
            out.append(Check(key, False, f"shell value: {e}", f"export {key}=<a valid value> in your shell profile"))
            continue
        try:
            n_claude = _norm(key, claude)
        except ValueError as e:
            out.append(Check(key, False, f"Claude Code value{where}: {e}",
                             f'fix "env".{key} in {target}, then restart Claude Code'))
            continue
        if n_claude == n_shell:
            shown = "set, not shown" if key in SECRET_KEYS and shell else (shell or "unset")
            out.append(Check(key, True, f"{shown} (shell and Claude Code)"))
            continue
        if key == "BENCHBOOK_DATA_DIR" and url_shell and url_claude:
            # both sides use a server, so the embedded data directory is not used (BENCHBOOK_DATABASE_URL is checked)
            out.append(Check(key, True, "not used: BENCHBOOK_DATABASE_URL is set in the shell and Claude Code"))
            continue
        detail = f"shell {_shown(key, shell)}, Claude Code {_shown(key, claude)}{where}"
        if key in SECRET_KEYS:
            if shell and claude:
                detail = "Claude Code uses a different database than this shell: " + detail
            fix = (f'set "env": {{"{key}": "<the {key} value of your shell>"}} in {target}, then restart '
                   "Claude Code" if shell
                   else f"export {key}='<the value in {target}>'   (add it to your shell profile)")
        else:
            fix = (f'set "env": {json.dumps({key: shell})} in {target}, then restart Claude Code' if shell
                   else f"export {key}={shlex.quote(claude or '')}   (add it to your shell profile)")
        out.append(Check(key, False, detail, fix))
    return out


def _parse_ts(line: str) -> float | None:
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _counts(msg: str, root: Path) -> bool:
    """A "no benchbook project" line from another repository is expected when the plugin is installed for the user and
    Claude Code runs elsewhere; only one from this repository means a report was lost here."""
    if not any(m in msg for m in FAILURE_MARKERS):
        return False
    if m := NO_PROJECT.search(msg):
        where = Path(m.group(1))
        return where == root or root in where.parents
    return True


def _spool_key(msg: str) -> str | None:
    """The spool (one per database target) a hook.log line is about, or None for a line about no spool in
    particular (a report that was never queued)."""
    m = SPOOL_KEY.search(msg)
    return m.group(1) if m else None


def check_hook_log(root: Path) -> Check:
    """For this shell's database: failure lines in hook.log since the later of 24 hours ago and the last clean
    replay of its spool, and the reports still waiting in that spool (dead/, or queued and not drained). hook.log
    is shared by every database: a line about another database's spool does not count, and a replay of another
    spool settles nothing here."""
    from .hooks import _spool_dir, _state_dir

    log = _state_dir() / "hook.log"
    spool = _spool_dir()
    mine = spool.name
    since = time.time() - RECENT_SECONDS
    failures: list[tuple[str | None, str]] = []
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        ts = _parse_ts(line)
        if ts is None or ts < since:
            continue
        msg = line[20:]
        key = _spool_key(msg)
        if key not in (None, mine):
            continue
        if msg.startswith("replay") and "dead 0; still queued 0" in msg:
            # a clean replay of this spool settles what came before it; a replay line that names no spool (an
            # older build) settles only the lines that name none either
            failures = [] if key == mine else [f for f in failures if f[0] is not None]
        elif _counts(msg, root):
            failures.append((key, line))
    dead = spool / "dead"
    n_dead = len(list(dead.glob("*.json"))) if dead.is_dir() else 0
    now = time.time()
    n_stuck = 0
    for p in spool.glob("*.json") if spool.is_dir() else []:
        try:
            n_stuck += now - p.stat().st_mtime > STUCK_SECONDS
        except OSError:
            continue
    if not failures and not n_dead and not n_stuck:
        return Check("hook failures", True, f"none in the last 24h; nothing waiting in {spool} ({log})")
    detail = (f"{len(failures)} failure line(s) in the last 24h, {n_dead} report(s) in {dead}, "
              f"{n_stuck} report(s) queued for over {STUCK_SECONDS // 60} min in {spool}")
    if failures:
        detail += f"\n      last: {failures[-1][1][:200]}"
    return Check("hook failures", False, detail, f"read {log}, fix the cause, then run: benchbook hook replay")


def _installed_plugin_paths() -> list[Path]:
    installed = _read_json(claude_dir() / "plugins" / "installed_plugins.json")
    plugins = installed.get("plugins", installed)
    entries = plugins.get(PLUGIN) if isinstance(plugins, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return []
    return [Path(e["installPath"]) for e in entries if isinstance(e, dict) and isinstance(e.get("installPath"), str)]


def check_plugin_hooks() -> Check | None:
    """An install from before the plugin's SubagentStop matcher was removed keeps running the hook only for
    benchbook-worker, whatever BENCHBOOK_HOOK_AGENTS says: Claude Code keeps an installed plugin version's files until
    the plugin is updated."""
    stale: list[Path] = []
    for path in _installed_plugin_paths():
        hooks = _read_json(path / "hooks" / "hooks.json").get("hooks")
        entries = hooks.get("SubagentStop") if isinstance(hooks, dict) else None
        if isinstance(entries, list) and any(isinstance(e, dict) and e.get("matcher") not in (None, "", "*")
                                             for e in entries):
            stale.append(path)
    if not stale:
        return None
    return Check("plugin hooks", False,
                 f"the installed plugin ({stale[0]}) runs the hook only for benchbook-worker, so BENCHBOOK_HOOK_AGENTS "
                 "cannot add other subagents",
                 f"claude plugin marketplace update benchbook && claude plugin update {PLUGIN},"
                 " then restart Claude Code")


def _guarded(name: str, fn: Any, *args: Any) -> list[Check]:
    """A check that raises must not take the others (or --json) down with it."""
    try:
        res = fn(*args)
    except Exception as e:  # noqa: BLE001
        return [Check(name, False, f"check failed: {type(e).__name__}: {e}")]
    if res is None:
        return []
    return res if isinstance(res, list) else [res]


def claude_code_checks(root: Path) -> list[Check]:
    checks: list[Check] = []
    if claude_dir().is_dir():
        checks += _guarded("benchbook on PATH", check_path, root)
        checks += _guarded("claude code plugin", check_plugin, root)
        checks += _guarded("plugin hooks", check_plugin_hooks)
        checks += _guarded("claude code env", check_env, root)
    else:
        checks.append(Check("claude code", True, f"{claude_dir()} not found; connection checks skipped"))
    checks += _guarded("hook failures", check_hook_log, root.resolve())
    return checks
