"""Short ids (spec §7): prefix + first 8 hex chars, e.g. e:91c0a2f1. Dependency-free."""
from __future__ import annotations

import re
from uuid import UUID

PREFIX = {"experiment": "e", "claim": "c", "task": "t", "source": "s", "decision": "d", "link": "l"}
TABLE = {"experiment": "experiments", "claim": "claims", "task": "tasks", "source": "sources",
         "decision": "decisions", "link": "links"}
KIND_BY_PREFIX = {v: k for k, v in PREFIX.items()}
SHORT_ID_RE = re.compile(r"\b([ectsdl]):([0-9a-f]{8})\b")


def short(kind: str, id: UUID | str) -> str:
    return f"{PREFIX[kind]}:{str(id)[:8]}"
