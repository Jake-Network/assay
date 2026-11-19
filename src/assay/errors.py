"""Exceptions raised by the SDK. Adapters (CLI, MCP, hooks) map these to user-facing errors."""
from __future__ import annotations


class AssayError(Exception):
    """A user error: bad input, unknown id, forbidden action. CLI exit code 1."""


class ValidationFailed(AssayError):
    """Input failed schema validation (P3). `problems` holds one line per violation."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("validation failed:\n" + "\n".join(f"- {p}" for p in problems))


class NotFound(AssayError):
    pass


class Ambiguous(AssayError):
    pass


class PermissionDenied(AssayError):
    pass


class LeaseLost(AssayError):
    def __init__(self, task_id: str):
        super().__init__(
            f"lease lost on task {task_id}: another agent holds it or it is finished. "
            "Do not discard results: record experiments without task_id."
        )


class InvalidTransition(AssayError):
    pass
