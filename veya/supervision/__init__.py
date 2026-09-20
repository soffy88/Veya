"""Veya Dual/Auto Supervision Runtime — canonical contracts, policy, P1/P2 layers.

This package adds *supervision* over the existing execution authorities
(GoalRun / MasterAgent / hicode / dsh / workers). It is not a second runtime:
one Mission, one state machine, one report/review schema, shared by all three
modes (external / internal / auto).
"""

from __future__ import annotations

from .evidence import build_execution_report
from .external import ExternalSupervisor, MissionNotFound
from .loop import MissionLoop
from .models import (
    TERMINAL_STATUSES,
    EscalationCode,
    ExecutionReport,
    ExecutionTask,
    ExecutorKind,
    LineageEntry,
    Mission,
    MissionBudget,
    MissionPolicies,
    MissionStatus,
    ReviewDecision,
    SideEffectClass,
    SupervisionMode,
    SupervisorReview,
)
from .policy import (
    DEFAULT_HINTS,
    ESCALATION_TRIGGERS,
    EXTERNAL_TO_INTERNAL_TRIGGERS,
    INTERNAL_TO_EXTERNAL_TRIGGERS,
    SELF_HANDLED_TRIGGERS,
    RouterHints,
    classify_escalation,
    fallback_mode,
    preferred_mode,
    requires_owner,
    switch_direction,
)
from .retask import RetaskOutcome, apply_review, plan_retask
from .reviewer import InternalSupervisor, ReviewContext, SupervisorUnavailable
from .router import RouterDecision, RouterRequest, SupervisionRouter
from .runner import canonical_runner, runner_with
from .store import MissionStore

__all__ = [
    "DEFAULT_HINTS",
    "ESCALATION_TRIGGERS",
    "EXTERNAL_TO_INTERNAL_TRIGGERS",
    "INTERNAL_TO_EXTERNAL_TRIGGERS",
    "SELF_HANDLED_TRIGGERS",
    "TERMINAL_STATUSES",
    "EscalationCode",
    "ExecutionReport",
    "ExecutionTask",
    "ExecutorKind",
    "ExternalSupervisor",
    "InternalSupervisor",
    "LineageEntry",
    "Mission",
    "MissionBudget",
    "MissionLoop",
    "MissionNotFound",
    "MissionPolicies",
    "MissionStatus",
    "MissionStore",
    "RetaskOutcome",
    "ReviewContext",
    "ReviewDecision",
    "RouterDecision",
    "RouterHints",
    "RouterRequest",
    "SideEffectClass",
    "SupervisionMode",
    "SupervisionRouter",
    "SupervisorReview",
    "SupervisorUnavailable",
    "apply_review",
    "build_execution_report",
    "canonical_runner",
    "classify_escalation",
    "fallback_mode",
    "plan_retask",
    "preferred_mode",
    "requires_owner",
    "runner_with",
    "switch_direction",
]
