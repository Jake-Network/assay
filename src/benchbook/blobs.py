"""Blob URIs. A stored file is named benchbook://blob/<sha256> in the artifacts of the record that carries it.

Records written before the rename say assay://blob/<sha256>, and the ledger is append-only: those rows are never
rewritten. So both schemes are read, for as long as old ledgers exist, and new records are written with benchbook://.
"""
from __future__ import annotations

from typing import Any

BLOB_PREFIX = "benchbook://blob/"
LEGACY_BLOB_PREFIX = "assay://blob/"
BLOB_PREFIXES = (BLOB_PREFIX, LEGACY_BLOB_PREFIX)


def is_blob_uri(uri: object) -> bool:
    return isinstance(uri, str) and uri.startswith(BLOB_PREFIXES)


def blob_digest(uri: str) -> str:
    """The sha256 a blob URI of either scheme names; anything else is returned unchanged."""
    for prefix in BLOB_PREFIXES:
        if uri.startswith(prefix):
            return uri[len(prefix):]
    return uri


def blob_uris(digest: str) -> tuple[str, str]:
    """Every URI a record may hold for this content, the current scheme first."""
    return BLOB_PREFIX + digest, LEGACY_BLOB_PREFIX + digest


def artifact_blob(artifacts: Any) -> str | None:
    """The first blob URI in a record's artifacts list, as stored."""
    for a in artifacts or []:
        if isinstance(a, dict) and is_blob_uri(a.get("uri")):
            return str(a["uri"])
    return None
