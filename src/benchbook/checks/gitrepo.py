"""Git plumbing for a verification: resolve a commit, read the declarations a commit keeps, the paths a range of
commits changed and the prefixes that cover them."""
from __future__ import annotations

import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..errors import BenchbookError

# Where a commit keeps its declarations; .assay/ is the folder's name before the rename.
CONFIG_PATHS = (".benchbook/config.toml", ".assay/config.toml")


# ---------------------------------------------------------------------------------------------------- git

def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", stdin=subprocess.DEVNULL)
    if check and p.returncode != 0:
        raise BenchbookError(f"git {' '.join(args)}: {p.stderr.strip() or f'exit {p.returncode}'}")
    return p


def toplevel(path: Path) -> Path:
    p = _git(path, "rev-parse", "--show-toplevel", check=False)
    if p.returncode != 0:
        raise BenchbookError(f"not inside a git repository: {path}")
    return Path(p.stdout.strip())


def resolve_commit(repo: Path, ref: str) -> str:
    """Step 1: the full sha of a commit-ish."""
    p = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", check=False)
    if p.returncode != 0 or not p.stdout.strip():
        raise BenchbookError(f"not a commit in {repo}: {ref!r}")
    return p.stdout.strip()


class _Unreadable(Exception):
    pass


def config_at(repo: Path, sha: str) -> tuple[dict[str, Any], str] | None:
    """The committed config.toml of a commit and its path, None when it has none. _Unreadable: not valid TOML."""
    for rel in CONFIG_PATHS:
        p = _git(repo, "show", f"{sha}:{rel}", check=False)
        if p.returncode == 0:
            try:
                return tomllib.loads(p.stdout), rel
            except tomllib.TOMLDecodeError as e:
                raise _Unreadable(f"{rel} at {sha[:12]}: {e}") from e
    return None


MAX_SCOPE_PATHS = 20


def _parent(prefix: str) -> str:
    """"a/b/c/" -> "a/b/", "a/" or "x.py" -> "" (the whole repository)."""
    head = prefix.rstrip("/").rpartition("/")[0]
    return head + "/" if head else ""


def path_prefixes(paths: Sequence[str], limit: int = MAX_SCOPE_PATHS) -> list[str]:
    """Changed files as the directory prefixes the brief scopes by (T06): each file's directory ("media/x.py" ->
    "media/"; a file at the root stays itself), prefixes inside another dropped, and the deepest lifted to their
    parent until at most limit remain. [""] is the whole repository."""
    def reduce(ps: set[str]) -> list[str]:
        if "" in ps:
            return [""]
        out: list[str] = []
        for p in sorted(ps):
            if not any(p.startswith(q) for q in out if q.endswith("/")):
                out.append(p)
        return out

    found = reduce({p.rsplit("/", 1)[0] + "/" if "/" in p else p for p in paths if p})
    while len(found) > limit:
        depth = max(p.rstrip("/").count("/") for p in found)
        found = reduce({_parent(p) if p.rstrip("/").count("/") == depth else p for p in found})
    return found


def changed_paths(repo: Path, merge_base: str, commit: str) -> list[str]:
    """Files that differ between the merge-base and the commit. --no-renames: a test moved out of a guarded folder
    must show up as the deletion it is, not only under its new name."""
    out = _git(repo, "diff", "--name-only", "--no-renames", "-z", merge_base, commit).stdout
    return [p for p in out.split("\0") if p]


def _git_in(repo: Path, stdin: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if p.returncode != 0:
        raise BenchbookError(f"git {' '.join(args)}: {p.stderr.strip() or f'exit {p.returncode}'}")
    return p.stdout


def merged_from(repo: Path, merge_base: str, commit: str, others: Sequence[str]) -> list[str]:
    """The commits of merge_base..commit that one of others (other pieces' reported commits) reaches, when that
    commit is itself in the range: the work merged in from other pieces, oldest first; [] when none is."""
    in_range = set(_git(repo, "rev-list", f"{merge_base}..{commit}").stdout.split())
    tips = [s for s in dict.fromkeys(others) if s in in_range and s != commit]
    if not tips:
        return []
    return _git(repo, "rev-list", "--reverse", *tips, f"^{merge_base}").stdout.split()


def trailer_pieces(repo: Path, commits: Sequence[str], key: str = "Benchbook-Piece") -> dict[str, set[str]]:
    """The piece names each commit's trailer carries."""
    if not commits:
        return {}
    fmt = f"%H%x00%(trailers:key={key},valueonly,separator=%x1f)%x1e"
    out = _git_in(repo, "".join(c + "\n" for c in commits), "log", "--no-walk=unsorted", f"--format={fmt}", "--stdin")
    found: dict[str, set[str]] = {}
    for entry in (e.strip("\n") for e in out.split("\x1e")):
        if entry:
            sha, _, values = entry.partition("\x00")
            found[sha.strip()] = {v.strip() for v in values.split("\x1f") if v.strip()}
    return found


def paths_of(repo: Path, merge_base: str, commit: str, leave_out: set[str]) -> list[str]:
    """Files changed by the commits of merge_base..commit other than merges and leave_out, newest first."""
    commits = [c for c in _git(repo, "rev-list", "--no-merges", f"{merge_base}..{commit}").stdout.split()
               if c not in leave_out]
    if not commits:
        return []
    out = _git_in(repo, "".join(c + "\n" for c in commits), "log", "--no-walk=unsorted", "--format=", "--name-only",
                  "--no-renames", "-z", "--stdin")
    return list(dict.fromkeys(f for f in (x.strip("\n") for x in out.split("\0")) if f))


def _diff_u0(repo: Path, merge_base: str, commit: str, path: str) -> str:
    return _git(repo, "diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", merge_base, commit,
                "--", path).stdout
