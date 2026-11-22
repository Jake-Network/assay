"""On-disk notes shared by boundary hooks and the Bash-result fast path."""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import config

DELIVERED = "delivered"
_SESSION = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def notes_dir(session_id: object) -> Path | None:
    """Where notes for this session wait (T11 writes them); None for a session id that is not a plain name."""
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id) or session_id in (".", ".."):
        return None
    return config.state_dir() / "notes" / session_id


def write_note(path: Path, line: str) -> None:
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def deliver(session_id: object, limit: int = 6000) -> str:
    """Read this session's pending notes in file-name order. Move each before reading so concurrent hooks deliver
    it only once."""
    d = notes_dir(session_id)
    if d is None:
        return ""
    try:
        with os.scandir(d) as entries:
            files = sorted(Path(entry.path) for entry in entries
                           if entry.name.endswith((".md", ".txt")) and entry.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return ""
    done = d / DELIVERED
    texts: list[str] = []
    size = 0
    for f in files:
        try:
            if size and size + f.stat().st_size > limit:
                break
            done.mkdir(exist_ok=True)
            target = done / f.name
            os.replace(f, target)
            text = target.read_text(encoding="utf-8").strip()
        except OSError:
            continue  # taken by another hook
        if text:
            texts.append(text)
            size += len(text) + 2
    combined = "\n\n".join(texts)
    if len(combined) <= limit:
        return combined
    cut = combined.rfind("\n", 0, limit - 2)
    return combined[:limit - 1] + "…" if cut <= 0 else combined[:cut] + "\n…"
