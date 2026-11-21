"""SDK input/output models (pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .ids import KIND_BY_PREFIX, PREFIX, TABLE, short  # noqa: F401  (re-exported)

Role = Literal["worker", "orchestrator", "human", "harness"]
RunStatus = Literal["ok", "error", "timeout", "invalid"]
Outcome = Literal["improved", "no_change", "worse", "failed", "unscored"]


class _M(BaseModel):
    model_config = ConfigDict(frozen=True)


class Project(_M):
    id: str
    title: str
    goal: str
    primary_metric: str | None
    metric_direction: Literal["max", "min"] | None
    improve_epsilon: float
    pack: str
    settings: dict[str, Any]
    created_at: datetime


class Task(_M):
    id: UUID
    project_id: str
    title: str
    spec: dict[str, Any]
    tags: list[str]
    priority: int
    depends_on: list[UUID]
    created_by: str
    status: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempts: int
    max_attempts: int
    summary: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def short_id(self) -> str:
        return short("task", self.id)


class ExperimentSummary(_M):
    id: UUID
    author: str
    seed: int | None
    run_status: str
    outcome: str
    primary_value: float | None
    notes: str | None
    created_at: datetime


class TriedResult(_M):
    dedup_key: str
    exact: list[ExperimentSummary]
    replicates: int
    best_value: float | None
    invalidated: list[ExperimentSummary] = []
    note: str | None = None


class RecordResult(_M):
    id: UUID | None
    outcome: str | None
    is_new_best: bool
    duplicate_of: UUID | None
    dedup_key: str
    primary_value: float | None = None


class ClaimResult(_M):
    id: UUID
    duplicate_of: UUID | None
    link_ids: list[UUID] = []


class Decision(_M):
    id: UUID
    target_type: str
    target_id: UUID
    new_status: str
    oracle: str
    decided_by: str
