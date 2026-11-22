"""`benchbook install` and `benchbook uninstall` (T07): one line in, one line out.

install, for Claude Code (the default): the CLI at this version (`uv tool install benchbook==<version>`), the
marketplace and the hook plugin (`benchbook@benchbook`; the MCP server is the separate `benchbook-mcp` plugin,
installed only with --mcp). Without a `claude` command the Claude Code steps are printed instead of run. For Codex
and Cursor (--harness) it merges the packaged hooks.json into the user's manifest, keeping the hooks already there.

uninstall removes the plugins (or prints how), the state directory's spool and logs, this checkout's .benchbook/ when
git does not track it and its .git/info/exclude line, and benchbook's entries in the Codex and Cursor manifests. The
embedded database is removed only with --purge. Every step is listed as it is done; --dry-run lists them only."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .errors import BenchbookError

MARKETPLACE = "Jake-Network/benchbook"
PLUGINS = ("benchbook@benchbook", "benchbook-mcp@benchbook")
LEGACY_PLUGIN = "assay@assay"
HARNESSES = ("claude-code", "codex", "cursor")


@dataclass
class Plan:
    """What install or uninstall does: commands to run and files to change, in order, and what happened."""
    dry_run: bool = False
    done: list[str] = field(default_factory=list)

    def run(self, argv: list[str], *, optional: bool = False) -> bool:
        """Run argv (or, dry or without the program, print it). optional: a failure is reported, not raised."""
        shown = " ".join(argv)
        if self.dry_run:
            self.done.append(f"would run: {shown}")
            return True
        if shutil.which(argv[0]) is None:
            self.done.append(f"run yourself ({argv[0]} not found): {shown}")
            return False
        p = subprocess.run(argv, capture_output=True, text=True)
        if p.returncode != 0:
            msg = f"failed (exit {p.returncode}): {shown}: {(p.stderr or p.stdout).strip()[-300:]}"
            if not optional:
                raise BenchbookError(msg)
            self.done.append(msg)
            return False
        self.done.append(f"ran: {shown}")
        return True

    def remove(self, path: Path, what: str) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if self.dry_run:
            self.done.append(f"would remove {what}: {path}")
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        self.done.append(f"removed {what}: {path}")


def version() -> str:
    from . import __version__

    return str(__version__)


def manifest(harness: str) -> dict[str, Any]:
    """The packaged hooks.json of a harness (the wheel's copy, else the repository's)."""
    here = Path(__file__).resolve().parent
    for p in (here / "manifests" / harness / "hooks.json", here.parents[1] / "integrations" / harness / "hooks.json"):
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    raise BenchbookError(f"no hooks.json for {harness} in this install")


def manifest_target(harness: str) -> Path:
    if harness == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"
    return Path.home() / ".cursor" / "hooks.json"


def _ours(entry: Any) -> bool:
    return isinstance(entry, dict) and any(isinstance(h, dict) and "benchbook hook" in str(h.get("command", ""))
                                           for h in entry.get("hooks", []) or [entry])


def merge_manifest(current: dict[str, Any], ours: dict[str, Any]) -> dict[str, Any]:
    """current with benchbook's entries replaced by ours; everyone else's hooks stay as they are."""
    out = {**current, **{k: v for k, v in ours.items() if k not in ("hooks", "description")}}
    hooks = {k: [e for e in v if not _ours(e)] for k, v in (current.get("hooks") or {}).items() if isinstance(v, list)}
    for event, entries in (ours.get("hooks") or {}).items():
        hooks.setdefault(event, []).extend(entries)
    out["hooks"] = hooks
    return out


def strip_manifest(current: dict[str, Any]) -> dict[str, Any]:
    hooks = {k: [e for e in v if not _ours(e)] for k, v in (current.get("hooks") or {}).items() if isinstance(v, list)}
    return {**current, "hooks": {k: v for k, v in hooks.items() if v}}


def _write_json(plan: Plan, path: Path, data: dict[str, Any], what: str) -> None:
    if plan.dry_run:
        plan.done.append(f"would write {what}: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    plan.done.append(f"wrote {what}: {path}")


def install(harness: str = "claude-code", *, dry_run: bool = False, mcp: bool = False,
            cli: bool = True) -> Plan:
    if harness not in HARNESSES:
        raise BenchbookError(f"--harness: one of {', '.join(HARNESSES)}")
    plan = Plan(dry_run)
    if cli:
        plan.run(["uv", "tool", "install", f"benchbook=={version()}"], optional=True)
    if harness == "claude-code":
        if plan.run(["claude", "plugin", "marketplace", "add", MARKETPLACE]):
            plan.run(["claude", "plugin", "install", PLUGINS[0]])
            if mcp:
                plan.run(["claude", "plugin", "install", PLUGINS[1]])
        elif not dry_run:
            plan.done.append(f"run yourself: claude plugin install {PLUGINS[0]}")
    else:
        target = manifest_target(harness)
        current = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
        _write_json(plan, target, merge_manifest(current, manifest(harness)), f"{harness} hooks")
    plan.done.append("next: cd <your repository> && benchbook init")
    return plan


def uninstall(root: Path | None, *, dry_run: bool = False, purge: bool = False) -> Plan:
    plan = Plan(dry_run)
    if shutil.which("claude") is not None or dry_run:
        for plugin in (*PLUGINS, LEGACY_PLUGIN):
            plan.run(["claude", "plugin", "uninstall", plugin], optional=True)
    else:
        plan.done.append(f"run yourself: claude plugin uninstall {PLUGINS[0]} (and {PLUGINS[1]} if installed)")
    for harness in ("codex", "cursor"):
        target = manifest_target(harness)
        if target.is_file():
            try:
                current = json.loads(target.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if any(_ours(e) for v in (current.get("hooks") or {}).values() if isinstance(v, list) for e in v):
                _write_json(plan, target, strip_manifest(current), f"{harness} hooks without benchbook")
    plan.remove(config.state_dir(), "the state directory (spool, hook log, notes)")
    if root is not None:
        local = config.local_dir(root)
        tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--", local.name], capture_output=True,
                                 text=True).stdout.strip()
        if local.is_dir() and not tracked:
            plan.remove(local, "this checkout's benchbook folder")
        _unexclude(plan, root, local.name)
    if purge:
        from .db.conn import data_dir

        plan.run(["benchbook", "db", "stop"], optional=True)
        plan.remove(data_dir(), "the embedded database")
    else:
        plan.done.append("kept the embedded database (benchbook uninstall --purge removes it)")
    plan.done.append("last: uv tool uninstall benchbook")
    return plan


def _unexclude(plan: Plan, root: Path, name: str) -> None:
    p = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"], capture_output=True,
                       text=True)
    if p.returncode != 0:
        return
    path = Path(p.stdout.strip())
    path = path if path.is_absolute() else root / path
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    keep = [x for x in lines if x.strip() not in (f"/{name}/", f"{name}/", f"/{name}", name)]
    if len(keep) == len(lines):
        return
    if plan.dry_run:
        plan.done.append(f"would remove the {name}/ line from {path}")
        return
    path.write_text("".join(keep), encoding="utf-8")
    plan.done.append(f"removed the {name}/ line from {path}")
