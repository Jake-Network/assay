"""Exceptions raised by the SDK. Adapters (CLI, MCP, hooks) map these to user-facing errors."""
from __future__ import annotations


class BenchbookError(Exception):
    """A user error: bad input, unknown id, forbidden action. CLI exit code 1."""


class ValidationFailed(BenchbookError):
    """Input failed schema validation (P3). `problems` holds one line per violation."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("validation failed:\n" + "\n".join(f"- {p}" for p in problems))


class NotFound(BenchbookError):
    pass


class Ambiguous(BenchbookError):
    pass


class PermissionDenied(BenchbookError):
    pass


class InvalidAgentToken(PermissionDenied):
    """The database rejected this token for the requested project."""


class LeaseLost(BenchbookError):
    def __init__(self, task_id: str):
        super().__init__(
            f"lease lost on task {task_id}: another agent holds it or it is finished. "
            "Do not discard results: record experiments without task_id."
        )


class InvalidTransition(BenchbookError):
    pass


class Conflict(BenchbookError):
    """Someone else is already doing this (a live intent with the same key). CLI exit code 2."""

    def __init__(self, message: str, existing: object = None, agent: str | None = None):
        self.existing = existing
        self.agent = agent
        super().__init__(message)
