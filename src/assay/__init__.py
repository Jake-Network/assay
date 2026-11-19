"""assay: your agents' lab notebook. Nothing they find gets lost, repeated, or trusted without evidence."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import AssayError, LeaseLost, NotFound, PermissionDenied, ValidationFailed

if TYPE_CHECKING:
    from .ledger import Ledger

__version__ = "0.1.0.dev0"
__all__ = ["Ledger", "AssayError", "ValidationFailed", "NotFound", "PermissionDenied", "LeaseLost"]


def __getattr__(name: str) -> Any:
    # Lazy: `assay hook` must start fast and never needs the SDK's heavier imports.
    if name == "Ledger":
        from .ledger import Ledger

        return Ledger
    raise AttributeError(name)
