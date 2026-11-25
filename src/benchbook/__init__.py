"""benchbook: your agents' lab notebook. Nothing they find gets lost, repeated, or trusted without evidence."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import BenchbookError, LeaseLost, NotFound, PermissionDenied, ValidationFailed

if TYPE_CHECKING:
    from .core.ledger import Ledger

try:
    from ._version import __version__  # written from git by the build (hatch-vcs)
except ImportError:  # a source tree that was never built
    __version__ = "0+unknown"
__all__ = ["__version__", "Ledger", "BenchbookError", "ValidationFailed", "NotFound", "PermissionDenied", "LeaseLost"]


def __getattr__(name: str) -> Any:
    # Lazy: `benchbook hook` must start fast and never needs the SDK's heavier imports.
    if name == "Ledger":
        from .core.ledger import Ledger

        return Ledger
    raise AttributeError(name)
