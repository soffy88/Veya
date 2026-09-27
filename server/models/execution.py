from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ExecutionMode(StrEnum):
    CONVERSATIONAL = "CONVERSATIONAL"
    STRUCTURED_CONSTRAINED = "STRUCTURED_CONSTRAINED"
    AUTO = "AUTO"


class PlanningPolicy(StrEnum):
    NORMAL = "NORMAL"
    PREPLANNED = "PREPLANNED"
    LOCKED_PLAN = "LOCKED_PLAN"


class ExecutionConstraints(BaseModel):
    planning_policy: PlanningPolicy = PlanningPolicy.NORMAL
    verification_policy: str = "DEFAULT"
    checkpoint_policy: str = "DEFAULT"
    execution_order_policy: str = "DEFAULT"
    retry_policy: str = "DEFAULT"
    completion_policy: str = "DEFAULT"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreplannedExecutionSpec(BaseModel):
    plan_id: str
    manifest_hash: str
    ordered_steps: list[dict[str, Any]]
    required_steps: list[str]
    constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)
    verification_requirements: list[str] = Field(default_factory=list)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalExecutionRequest(BaseModel):
    source: str
    mode: ExecutionMode
    objective: str
    project_root: str = "."
    constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)
    preplanned_spec: PreplannedExecutionSpec | None = None
    session_id: str | None = None
    capability: str | None = None


class CanonicalContinuationRef(BaseModel):
    session_id: str
    goal_run_id: str
    checkpoint_id: str | None = None
    revision: int | None = None
    resume: bool = True
