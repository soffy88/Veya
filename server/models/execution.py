from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

class ExecutionMode(str, Enum):
    CONVERSATIONAL = "CONVERSATIONAL"
    STRUCTURED_CONSTRAINED = "STRUCTURED_CONSTRAINED"
    AUTO = "AUTO"

class PlanningPolicy(str, Enum):
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
    metadata: Dict[str, Any] = Field(default_factory=dict)

class PreplannedExecutionSpec(BaseModel):
    plan_id: str
    manifest_hash: str
    ordered_steps: List[Dict[str, Any]]
    required_steps: List[str]
    constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)
    verification_requirements: List[str] = Field(default_factory=list)
    source_metadata: Dict[str, Any] = Field(default_factory=dict)

class CanonicalExecutionRequest(BaseModel):
    source: str
    mode: ExecutionMode
    objective: str
    project_root: str = "."
    constraints: ExecutionConstraints = Field(default_factory=ExecutionConstraints)
    preplanned_spec: Optional[PreplannedExecutionSpec] = None
    session_id: Optional[str] = None
    capability: Optional[str] = None

class CanonicalContinuationRef(BaseModel):
    session_id: str
    goal_run_id: str
    checkpoint_id: Optional[str] = None
    revision: Optional[int] = None
    resume: bool = True
