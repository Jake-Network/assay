"""`benchbook install` and `benchbook uninstall` (T07): one line in, one line out.

install, for Claude Code (the default): the CLI at this version (`uv tool install benchbook==<version>` for a release,
the git repository for a development build, which is not on PyPI), the
marketplace and the hook plugin (`benchbook@benchbook`; the MCP server is the separate `benchbook-mcp` plugin,
installed only with --mcp). Without a `claude` command the Claude Code steps are printed instead of run. For Codex,
Cursor and Gemini CLI, it merges the packaged hooks into the user's settings.

uninstall removes the plugins (or prints how), the state directory's spool and logs, this checkout's .benchbook/ when
git answers that it tracks nothing there (a failing git, a broken index or a directory outside git keeps it) and its
.git/info/exclude line, and benchbook's entries in installed hook manifests. The embedded database is removed only
with --purge, and only when its directory holds a PostgreSQL cluster whose server is confirmed stopped.
Every step is listed as it is done; --dry-run lists them only."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..errors import BenchbookError

MARKETPLACE = "Jake-Network/benchbook"
PLUGINS = ("benchbook@benchbook", "benchbook-mcp@benchbook")
LEGACY_PLUGIN = "assay@assay"
HARNESSES = ("claude-code", "codex", "cursor", "gemini")


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
    from .. import __version__

    return str(__version__)


GIT_SOURCE = "git+https://github.com/Jake-Network/benchbook"
RELEASE = re.compile(r"\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?")
_UNREAD = object()


def install_source() -> dict[str, Any] | None:
    """Where this benchbook was installed from (PEP 610 direct_url.json); None for an index install or when unknown."""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        raw = distribution("benchbook").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def cli_install_command(v: str | None = None, source: Any = _UNREAD) -> list[str] | None:
    """The command that installs this build's CLI again, or None when no source reproduces it.

    A release is on PyPI under its version. A development build (`0.1.devN+g<commit>`) never is: it is reinstalled
    from the git repository and commit it was installed from. A build from a local directory, a modified checkout or
    an unknown source has nothing to pin, and its CLI is left as it is."""
    v = version() if v is None else v
    if RELEASE.fullmatch(v):
        return ["uv", "tool", "install", f"benchbook=={v}"]
    src = install_source() if source is _UNREAD else source
    vcs = (src or {}).get("vcs_info") or {}
    url, commit = (src or {}).get("url"), vcs.get("commit_id")
    if vcs.get("vcs") == "git" and isinstance(url, str) and url and isinstance(commit, str) and commit:
        return ["uv", "tool", "install", "--from", f"git+{url}@{commit}", "benchbook"]
    return None


def manifest(harness: str) -> dict[str, Any]:
    """The packaged hooks.json of a harness (the wheel's copy, else the repository's)."""
    here = Path(__file__).resolve().parents[1]  # the benchbook package
    for p in (here / "manifests" / harness / "hooks.json", here.parents[1] / "integrations" / harness / "hooks.json"):
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    raise BenchbookError(f"no hooks.json for {harness} in this install")


def manifest_target(harness: str) -> Path:
    if harness == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"
    if harness == "gemini":
        return Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()) / ".gemini" / "settings.json"
    return Path.home() / ".cursor" / "hooks.json"


def _ours(entry: Any) -> bool:
    return isinstance(entry, dict) and "benchbook hook" in str(entry.get("command", ""))


def merge_manifest(current: dict[str, Any], ours: dict[str, Any]) -> dict[str, Any]:
    """current with benchbook's entries replaced by ours; everyone else's hooks stay as they are."""
    out = {**current, **{k: v for k, v in ours.items() if k not in ("hooks", "description")}}
    hooks = strip_manifest(current)["hooks"]
    for event, entries in (ours.get("hooks") or {}).items():
        hooks.setdefault(event, []).extend(entries)
    out["hooks"] = hooks
    return out


def strip_manifest(current: dict[str, Any]) -> dict[str, Any]:
    hooks = {}
    for event, entries in (current.get("hooks") or {}).items():
        if not isinstance(entries, list):
            hooks[event] = entries
            continue
        keep = []
        for entry in entries:
            handlers = entry.get("hooks") if isinstance(entry, dict) else None
            if isinstance(handlers, list):
                other = [handler for handler in handlers if not _ours(handler)]
                if other or not handlers:
                    keep.append({**entry, "hooks": other})
            elif not _ours(entry):
                keep.append(entry)
        if keep or not entries:
            hooks[event] = keep
    return {**current, "hooks": hooks}


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
        if (cmd := cli_install_command()) is not None:
            plan.run(cmd, optional=True)
        else:
            plan.done.append(f"kept the current benchbook CLI: build {version()} has no source to install it from "
                             f"again (a local or modified checkout); install one with uv tool install --from "
                             f"{GIT_SOURCE} benchbook, or pass --no-cli")
    if harness == "claude-code":
        plugins = [PLUGINS[0], *([PLUGINS[1]] if mcp else [])]
        if plan.run(["claude", "plugin", "marketplace", "add", MARKETPLACE]):
            for name in plugins:
                plan.run(["claude", "plugin", "install", name])
        elif not dry_run:
            plan.done.append("run yourself: " + " && ".join(
                [f"claude plugin marketplace add {MARKETPLACE}", *(f"claude plugin install {n}" for n in plugins)]))
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
    for harness in HARNESSES[1:]:
        target = manifest_target(harness)
        if target.is_file():
            try:
                current = json.loads(target.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if current.get("hooks") and (other := strip_manifest(current)) != current:
                _write_json(plan, target, other, f"{harness} hooks without benchbook")
    plan.remove(config.state_dir(), "the state directory (spool, hook log, notes)")
    if root is not None:
        local = config.local_dir(root)
        if local.is_dir():
            untracked, why = _untracked(root, local.name)
            if untracked:
                plan.remove(local, "this checkout's benchbook folder")
            else:
                plan.done.append(f"kept {local}: {why}")
        _unexclude(plan, root, local.name)
    if purge:
        _purge_database(plan)
    else:
        plan.done.append("kept the embedded database (benchbook uninstall --purge removes it)")
    plan.done.append("last: uv tool uninstall benchbook")
    return plan


def _untracked(root: Path, name: str) -> tuple[bool, str]:
    """(True, "") only when git answers, and answers that it tracks nothing under root/name. A failing git (outside
    a repository, a corrupt index) is no answer: the folder may hold tracked files."""
    try:
        p = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", name], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
    except OSError as e:
        return False, f"git could not be run ({e}), so whether it is tracked is unknown"
    if p.returncode != 0:
        err = (p.stderr or p.stdout).strip().splitlines()
        return False, (f"git ls-files failed (exit {p.returncode}{': ' + err[0][:200] if err else ''}), so whether it "
                       "is tracked is unknown")
    if p.stdout.strip("\0"):
        return False, "git tracks files in it"
    return True, ""


def _purge_database(plan: Plan) -> None:
    """--purge: remove the embedded cluster, only when the data directory is one and its server is
    confirmed stopped. BENCHBOOK_DATA_DIR can name any directory; a directory that is not a cluster is kept."""
    from ..db.conn import data_dir, is_cluster, stop_embedded

    d = data_dir()
    if not d.exists() and not d.is_symlink():
        plan.done.append(f"no embedded database at {d}")
        return
    if d.is_symlink() or not is_cluster(d):
        plan.done.append(f"kept {d}: not a PostgreSQL data directory, so --purge leaves it")
        return
    if plan.dry_run:
        plan.done.append(f"would stop the embedded database in {d}")
        plan.remove(d, "the embedded database")
        return
    try:
        if not stop_embedded(d):
            plan.done.append(f"kept {d}: the PostgreSQL data directory could not be confirmed before stopping it")
            return
    except Exception as e:  # noqa: BLE001 - whatever went wrong, the cluster is not known to be stopped
        plan.done.append(f"kept the embedded database {d}: its server was not confirmed stopped "
                         f"({type(e).__name__}: {str(e).strip()[:300]}); run benchbook db stop, then --purge again")
        return
    plan.done.append(f"stopped the embedded database in {d}")
    plan.remove(d, "the embedded database")


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
