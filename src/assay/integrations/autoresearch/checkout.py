"""The autoresearch checkout: files, partition, git, local state (§6.1.2, §6.2)."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...errors import AssayError
from .code import code_hash, extract_knobs, short_hash

STATE_REL = Path(".assay") / "ar_state.json"
PENDING_REL = Path(".assay") / "ar_pending.jsonl"


@dataclass
class Checkout:
    root: Path

    @classmethod
    def find(cls, start: Path | None = None) -> Checkout:
        root = (start or Path.cwd()).resolve()
        missing = [f for f in ("train.py", "prepare.py") if not (root / f).is_file()]
        if missing:
            raise AssayError(f"not an autoresearch checkout: {', '.join(missing)} missing in {root}. "
                             "Run assay ar from the directory that holds train.py and prepare.py.")
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

    # ------------------------------------------------------------------ git

    def git(self, *args: str, check: bool = False) -> subprocess.CompletedProcess[bytes] | None:
        try:
            r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        if check and r.returncode != 0:
            raise AssayError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace').strip()}")
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

    def parent(self, current: bytes) -> tuple[str | None, dict[str, Any] | None, str]:
        """(parent code hash, parent knobs, how). §6.1.2: committed -> HEAD~1, uncommitted -> HEAD,
        no git -> the last code this checkout synced or kept (.assay/ar_state.json)."""
        head = self.git_show("HEAD") if self.is_git() else None
        if head is not None:
            if code_hash(head) == code_hash(current):
                prev = self.git_show("HEAD~1")
                if prev is None:
                    return None, None, "root commit"
                return code_hash(prev), extract_knobs(prev), "HEAD~1"
            return code_hash(head), extract_knobs(head), "HEAD"
        st = self.load_state()
        if st.get("last_code_hash"):
            return st["last_code_hash"], st.get("last_knobs"), "state"
        return None, None, "none"

    # ------------------------------------------------------------------ local state

    def load_state(self) -> dict[str, Any]:
        p = self.root / STATE_REL
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, **updates: Any) -> None:
        p = self.root / STATE_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {**self.load_state(), **updates}
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, p)

    def pending_path(self) -> Path:
        return self.root / PENDING_REL
