"""veya.autonomous — Autonomous Agent V1 Package.

Architecture constraints:
- MasterAgent = sole semantic authority
- GoalRun = durable execution authority
- AgentRuntime = infrastructure authority only
- Execution Contract = frozen execution substrate
"""

from __future__ import annotations

from .assessor import SituationAssessor
from .cycle import AutonomousCycle
from .decision import DecisionPreconditions, DecisionStore
from .detectors import NoProgressDetector, NoProgressSignal, OscillationDetector, OscillationSignal
from .escalation import EscalationManager, InterruptHandler
from .evaluator import CompletionGate, OutcomeEvaluator
from .journal import ObservationJournal, reconcile_context
from .models import (
    ActionProposal,
    AutonomousDecision,
    AutonomousState,
    AutonomousStatus,
    BudgetState,
    CompletionDecision,
    DecisionType,
    DependencyStatus,
    EscalationReason,
    EscalationRequest,
    EvaluationResult,
    ExternalDependency,
    InterruptCategory,
    MissionRevision,
    Observation,
    ObservationSource,
    ObservationStatus,
    OutcomeVerdict,
    PlanProposal,
    ProgressAssessment,
    RiskLevel,
    SituationAssessment,
    WaitCondition,
    WaitType,
)
from .planner_adapter import AutonomousPlannerAdapter
from .reconciler import GoalReconciler, ReplanResult, RetaskResult
from .risk import AutonomousRiskGate, BudgetController
from .wait import ExternalDependencyTracker, WaitConditionManager

__all__ = [
    "ActionProposal",
    "AutonomousCycle",
    "AutonomousDecision",
    "AutonomousPlannerAdapter",
    "AutonomousRiskGate",
    "AutonomousState",
    "AutonomousStatus",
    "BudgetController",
    "BudgetState",
    "CompletionDecision",
    "CompletionGate",
    "DecisionPreconditions",
    "DecisionStore",
    "DecisionType",
    "DependencyStatus",
    "EscalationManager",
    "EscalationReason",
    "EscalationRequest",
    "EvaluationResult",
    "ExternalDependency",
    "ExternalDependencyTracker",
    "GoalReconciler",
    "InterruptCategory",
    "InterruptHandler",
    "MissionRevision",
    "NoProgressDetector",
    "NoProgressSignal",
    "Observation",
    "ObservationJournal",
    "ObservationSource",
    "ObservationStatus",
    "OscillationDetector",
    "OscillationSignal",
    "OutcomeEvaluator",
    "OutcomeVerdict",
    "PlanProposal",
    "ProgressAssessment",
    "ReplanResult",
    "RetaskResult",
    "RiskLevel",
    "SituationAssessment",
    "SituationAssessor",
    "WaitCondition",
    "WaitConditionManager",
    "WaitType",
    "reconcile_context",
]
