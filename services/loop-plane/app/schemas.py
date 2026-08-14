"""loop-plane schemas — Pydantic ≡ OpenAPI。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# State API
# ---------------------------------------------------------------------------


class TodoSpec(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class CreateGoalBody(BaseModel):
    objective: str = Field(min_length=1)
    todos: list[TodoSpec] = Field(default_factory=list)
    trace_id: str = ""


class TodoUpdateBody(BaseModel):
    status: Literal["open", "in_progress", "done", "blocked"]
    evidence: str = ""


class ClaimBody(BaseModel):
    lease_min: int = 45


class GateCheckBody(BaseModel):
    gate_scope: str


class TerminalCheckBody(BaseModel):
    action: str


class SpendBody(BaseModel):
    todo_id: str
    slots: int = 1


# ---------------------------------------------------------------------------
# Causal API
# ---------------------------------------------------------------------------


class PlanGoalBody(BaseModel):
    goal: str = Field(min_length=1)
    criteria: str = ""
    execute: bool = False


class DiagnoseBody(BaseModel):
    symptom: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Exec API
# ---------------------------------------------------------------------------


class DispatchBody(BaseModel):
    mode: Literal["sandbox", "shadow", "live_canary"] = "sandbox"
    tool_name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sched API
# ---------------------------------------------------------------------------


class SchedJobBody(BaseModel):
    name: str = Field(min_length=1)
    cron: str = ""
    pattern: str = ""
    action: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ClaimBody",
    "CreateGoalBody",
    "DiagnoseBody",
    "DispatchBody",
    "GateCheckBody",
    "PlanGoalBody",
    "SchedJobBody",
    "SpendBody",
    "TerminalCheckBody",
    "TodoSpec",
    "TodoUpdateBody",
]
