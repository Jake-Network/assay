"""The autoresearch checkout: files, partition, git, local state (§6.1.2, §6.2)."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ... import config
from ...errors import BenchbookError
from .code import code_hash, code_hashes, extract_knobs, short_hash

# files in the checkout's benchbook folder (config.local_dir: .benchbook, or .assay from before the rename)
STATE_FILE = "ar_state.json"
PENDING_FILE = "ar_pending.jsonl"


@dataclass
class Checkout:
    root: Path

    @classmethod
    def find(cls, start: Path | None = None) -> Checkout:
        root = (start or Path.cwd()).resolve()
        missing = [f for f in ("train.py", "prepare.py") if not (root / f).is_file()]
        if missing:
            raise BenchbookError(f"not an autoresearch checkout: {', '.join(missing)} missing in {root}. "
                             "Run benchbook ar from the directory that holds train.py and prepare.py.")
        return cls(root)

    @property
    def train_path(self) -> Path:
        return self.root / "train.py"

    def train_bytes(self) -> bytes:
        return self.train_path.read_bytes()

    def harness_hash(self) -> str:
        return code_hash((self.root / "prepare.py").read_bytes())

    def partition(self, hw: str) -> str:
        return f"{hw}/{short_hash(self.harness_hash())}"

    def partition_keys(self, hw: str) -> tuple[str, ...]:
        return partition_keys(hw, (self.root / "prepare.py").read_bytes())

    # ------------------------------------------------------------------ git

    def git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[bytes] | None:
        try:
            r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if check and r.returncode != 0:
            raise BenchbookError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
        return r

    def is_git(self) -> bool:
        r = self.git("rev-parse", "--is-inside-work-tree")
        return r is not None and r.returncode == 0 and r.stdout.strip() == b"true"

    def git_show(self, rev: str, path: str = "train.py") -> bytes | None:
        r = self.git("show", f"{rev}:./{path}")
        return r.stdout if r is not None and r.returncode == 0 else None

    def short_commit(self) -> str | None:
        r = self.git("rev-parse", "--short=7", "HEAD")
        return r.stdout.decode().strip() if r is not None and r.returncode == 0 else None

    def parent(self, current: bytes) -> tuple[tuple[str, ...], dict[str, Any] | None, str]:
        """(parent code hashes, parent knobs, how). §6.1.2: committed -> HEAD~1, uncommitted -> HEAD,
        no git -> the last code this checkout synced or kept (.benchbook/ar_state.json)."""
        head = self.git_show("HEAD") if self.is_git() else None
        if head is not None:
            if code_hash(head) == code_hash(current):
                prev = self.git_show("HEAD~1")
                if prev is None:
                    return (), None, "root commit"
                return code_hashes(prev), extract_knobs(prev), "HEAD~1"
            return code_hashes(head), extract_knobs(head), "HEAD"
        st = self.load_state()
        if st.get("last_code_hash"):
            return tuple(st.get("last_code_hashes") or [st["last_code_hash"]]), st.get("last_knobs"), "state"
        return (), None, "none"

    # ------------------------------------------------------------------ local state

    def _local(self, name: str, *leftovers: str) -> Path:
        """A file in the checkout's benchbook folder. While the new folder has none, a copy in the pre-rename folder
        is the one in use: after `git mv .assay .benchbook` in one worktree and a pull in another, the tracked
        config moves and the untracked state and queued runs stay behind in .assay/. leftovers: glob patterns of
        files that also count as the old copy being in use."""
        p = config.local_dir(self.root) / name
        old = self.root / config.LEGACY_CONFIG_DIR
        if p.parent != old and not p.exists() and old.is_dir():
            if (old / name).exists() or any(f for pattern in leftovers for f in old.glob(pattern)):
                return old / name
        return p

    def state_path(self) -> Path:
        return self._local(STATE_FILE)

    def load_state(self) -> dict[str, Any]:
        p = self.state_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, **updates: Any) -> None:
        p = self.state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {**self.load_state(), **updates}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, p)

    def pending_path(self) -> Path:
        # a batch a crashed flush left behind is still queued work
        return self._local(PENDING_FILE, PENDING_FILE + ".*.flushing")


def partition_keys(hw: str, source: bytes) -> tuple[str, ...]:
    return tuple(dict.fromkeys(f"{hw}/{short_hash(h)}" for h in code_hashes(source)))
