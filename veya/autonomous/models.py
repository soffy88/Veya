"""Canonical Data Models for Veya Autonomous Agent V1 (AA-P0).

Invariants:
- MasterAgent = sole semantic authority
- GoalRun = durable execution authority
- AgentRuntime = infrastructure authority only
- Execution Contract = frozen execution substrate
- No second state machine: AutonomousState reflects MasterAgent's cognitive stage.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AutonomousStatus(StrEnum):
    """MasterAgent autonomous cognitive stage (spec §3)."""

    OBSERVING = "OBSERVING"
    ASSESSING = "ASSESSING"
    PLANNING = "PLANNING"
    ACTING = "ACTING"
    VERIFYING = "VERIFYING"
    WAITING = "WAITING"
    ESCALATING = "ESCALATING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class ObservationSource(StrEnum):
    """Observation provenance source (spec §4)."""

    USER = "USER"
    EXECUTION = "EXECUTION"
    TOOL = "TOOL"
    VEYA_EVENT = "VEYA_EVENT"
    JEV = "JEV"
    CHANNEL = "CHANNEL"
    SCHEDULE = "SCHEDULE"
    EXTERNAL_EVENT = "EXTERNAL_EVENT"
    SYSTEM = "SYSTEM"


class ObservationStatus(StrEnum):
    """Freshness and reconciliation status of an observation (spec §24)."""

    CURRENT = "CURRENT"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"
    UNVERIFIED = "UNVERIFIED"


@dataclass
class Observation:
    """Canonical observation model (spec §4)."""

    observation_id: str = field(default_factory=lambda: f"obs_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    source: ObservationSource = ObservationSource.SYSTEM
    source_ref: str = ""
    kind: str = "general"
    payload_ref: str = ""
    summary: str = ""
    confidence: float = 1.0
    freshness: float = field(default_factory=time.time)
    observed_at: float = field(default_factory=time.time)
    dedup_key: str = ""
    status: ObservationStatus = ObservationStatus.CURRENT
    details: dict[str, Any] = field(default_factory=dict)
    supersedes: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)
    confirms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "mission_id": self.mission_id,
            "source": str(self.source),
            "source_ref": self.source_ref,
            "kind": self.kind,
            "payload_ref": self.payload_ref,
            "summary": self.summary,
            "confidence": self.confidence,
            "freshness": self.freshness,
            "observed_at": self.observed_at,
            "dedup_key": self.dedup_key,
            "status": str(self.status),
            "details": dict(self.details),
            "supersedes": list(self.supersedes),
            "contradicts": list(self.contradicts),
            "confirms": list(self.confirms),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Observation:
        source_val = data.get("source", "SYSTEM")
        try:
            source = ObservationSource(str(source_val).upper())
        except ValueError:
            source = ObservationSource.SYSTEM
        status_val = data.get("status", "CURRENT")
        try:
            status = ObservationStatus(str(status_val).upper())
        except ValueError:
            status = ObservationStatus.CURRENT

        return cls(
            observation_id=str(data.get("observation_id") or f"obs_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            source=source,
            source_ref=str(data.get("source_ref") or ""),
            kind=str(data.get("kind") or "general"),
            payload_ref=str(data.get("payload_ref") or ""),
            summary=str(data.get("summary") or ""),
            confidence=float(data.get("confidence", 1.0)),
            freshness=float(data.get("freshness", time.time())),
            observed_at=float(data.get("observed_at", time.time())),
            dedup_key=str(data.get("dedup_key") or ""),
            status=status,
            details=dict(data.get("details") or {}),
            supersedes=list(data.get("supersedes") or []),
            contradicts=list(data.get("contradicts") or []),
            confirms=list(data.get("confirms") or []),
        )


@dataclass
class SituationAssessment:
    """Canonical situation assessment model (spec §6)."""

    assessment_id: str = field(default_factory=lambda: f"sa_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    cycle_id: str = "cycle_1"
    known_facts: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    blocking_conditions: list[str] = field(default_factory=list)
    progress_state: str = "IN_PROGRESS"
    candidate_next_moves: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assessment_id": self.assessment_id,
            "mission_id": self.mission_id,
            "cycle_id": self.cycle_id,
            "known_facts": list(self.known_facts),
            "uncertainties": list(self.uncertainties),
            "constraints": list(self.constraints),
            "risks": list(self.risks),
            "blocking_conditions": list(self.blocking_conditions),
            "progress_state": self.progress_state,
            "candidate_next_moves": list(self.candidate_next_moves),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SituationAssessment:
        return cls(
            assessment_id=str(data.get("assessment_id") or f"sa_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            cycle_id=str(data.get("cycle_id") or "cycle_1"),
            known_facts=list(data.get("known_facts") or []),
            uncertainties=list(data.get("uncertainties") or []),
            constraints=list(data.get("constraints") or []),
            risks=list(data.get("risks") or []),
            blocking_conditions=list(data.get("blocking_conditions") or []),
            progress_state=str(data.get("progress_state") or "IN_PROGRESS"),
            candidate_next_moves=list(data.get("candidate_next_moves") or []),
            created_at=float(data.get("created_at", time.time())),
        )


class DecisionType(StrEnum):
    """Autonomous decision outcomes (spec §7)."""

    ACT = "ACT"
    WAIT = "WAIT"
    REPLAN = "REPLAN"
    RETASK = "RETASK"
    REQUEST_INFORMATION = "REQUEST_INFORMATION"
    ESCALATE = "ESCALATE"
    COMPLETE = "COMPLETE"
    ABORT = "ABORT"


@dataclass
class AutonomousDecision:
    """Canonical decision record (spec §7)."""

    decision_id: str = field(default_factory=lambda: f"dec_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    goal_run_id: str = "goal_run_1"
    cycle_id: str = "cycle_1"
    decision_type: DecisionType = DecisionType.ACT
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 1.0
    selected_action: str = ""
    alternatives_considered: list[str] = field(default_factory=list)
    expected_result: str = ""
    verification_plan: str = ""
    wake_condition: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "mission_id": self.mission_id,
            "goal_run_id": self.goal_run_id,
            "cycle_id": self.cycle_id,
            "decision_type": str(self.decision_type),
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "confidence": self.confidence,
            "selected_action": self.selected_action,
            "alternatives_considered": list(self.alternatives_considered),
            "expected_result": self.expected_result,
            "verification_plan": self.verification_plan,
            "wake_condition": self.wake_condition,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutonomousDecision:
        dtype_val = data.get("decision_type", "ACT")
        try:
            decision_type = DecisionType(str(dtype_val).upper())
        except ValueError:
            decision_type = DecisionType.ACT
        return cls(
            decision_id=str(data.get("decision_id") or f"dec_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            goal_run_id=str(data.get("goal_run_id") or "goal_run_1"),
            cycle_id=str(data.get("cycle_id") or "cycle_1"),
            decision_type=decision_type,
            reason=str(data.get("reason") or ""),
            evidence_refs=list(data.get("evidence_refs") or []),
            confidence=float(data.get("confidence", 1.0)),
            selected_action=str(data.get("selected_action") or ""),
            alternatives_considered=list(data.get("alternatives_considered") or []),
            expected_result=str(data.get("expected_result") or ""),
            verification_plan=str(data.get("verification_plan") or ""),
            wake_condition=data.get("wake_condition"),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class ActionProposal:
    """Action proposed by MasterAgent to GoalRun/Execution (spec §28)."""

    action_id: str = field(default_factory=lambda: f"act_{uuid.uuid4().hex[:12]}")
    decision_id: str = ""
    goal_id: str = ""
    action_type: str = "EXECUTE"
    executor_requirements: dict[str, Any] = field(default_factory=dict)
    workspace_requirements: dict[str, Any] = field(default_factory=dict)
    capability_requirements: list[str] = field(default_factory=list)
    expected_result: str = ""
    verification_plan: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "decision_id": self.decision_id,
            "goal_id": self.goal_id,
            "action_type": self.action_type,
            "executor_requirements": dict(self.executor_requirements),
            "workspace_requirements": dict(self.workspace_requirements),
            "capability_requirements": list(self.capability_requirements),
            "expected_result": self.expected_result,
            "verification_plan": self.verification_plan,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionProposal:
        return cls(
            action_id=str(data.get("action_id") or f"act_{uuid.uuid4().hex[:12]}"),
            decision_id=str(data.get("decision_id") or ""),
            goal_id=str(data.get("goal_id") or ""),
            action_type=str(data.get("action_type") or "EXECUTE"),
            executor_requirements=dict(data.get("executor_requirements") or {}),
            workspace_requirements=dict(data.get("workspace_requirements") or {}),
            capability_requirements=list(data.get("capability_requirements") or []),
            expected_result=str(data.get("expected_result") or ""),
            verification_plan=str(data.get("verification_plan") or ""),
            payload=dict(data.get("payload") or {}),
        )


@dataclass
class PlanProposal:
    """Planner output proposal awaiting MasterAgent authority (spec §31)."""

    plan_id: str = field(default_factory=lambda: f"plan_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    revision_id: str = "rev_1"
    assumptions: list[str] = field(default_factory=list)
    goals: list[dict[str, Any]] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_strategy: str = ""
    risk_notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "mission_id": self.mission_id,
            "revision_id": self.revision_id,
            "assumptions": list(self.assumptions),
            "goals": list(self.goals),
            "dependencies": {k: list(v) for k, v in self.dependencies.items()},
            "acceptance_criteria": list(self.acceptance_criteria),
            "verification_strategy": self.verification_strategy,
            "risk_notes": list(self.risk_notes),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanProposal:
        return cls(
            plan_id=str(data.get("plan_id") or f"plan_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            revision_id=str(data.get("revision_id") or "rev_1"),
            assumptions=list(data.get("assumptions") or []),
            goals=list(data.get("goals") or []),
            dependencies={k: list(v) for k, v in dict(data.get("dependencies") or {}).items()},
            acceptance_criteria=list(data.get("acceptance_criteria") or []),
            verification_strategy=str(data.get("verification_strategy") or ""),
            risk_notes=list(data.get("risk_notes") or []),
            created_at=float(data.get("created_at", time.time())),
        )


class OutcomeVerdict(StrEnum):
    """Recommendation by OutcomeEvaluator (spec §12)."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    PARTIAL = "PARTIAL"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"


@dataclass
class EvaluationResult:
    """Outcome evaluator recommendation (spec §12)."""

    evaluation_id: str = field(default_factory=lambda: f"eval_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    action_id: str = ""
    verdict: OutcomeVerdict = OutcomeVerdict.ACCEPT
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    verified_claims: list[str] = field(default_factory=list)
    unverified_claims: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "mission_id": self.mission_id,
            "action_id": self.action_id,
            "verdict": str(self.verdict),
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "verified_claims": list(self.verified_claims),
            "unverified_claims": list(self.unverified_claims),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationResult:
        v_val = data.get("verdict", "ACCEPT")
        try:
            verdict = OutcomeVerdict(str(v_val).upper())
        except ValueError:
            verdict = OutcomeVerdict.ACCEPT
        return cls(
            evaluation_id=str(data.get("evaluation_id") or f"eval_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            action_id=str(data.get("action_id") or ""),
            verdict=verdict,
            reason=str(data.get("reason") or ""),
            evidence_refs=list(data.get("evidence_refs") or []),
            verified_claims=list(data.get("verified_claims") or []),
            unverified_claims=list(data.get("unverified_claims") or []),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class ProgressAssessment:
    """Semantic progress assessment model (spec §13)."""

    progress_id: str = field(default_factory=lambda: f"prog_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    goal_run_id: str = "goal_run_1"
    objective_coverage: float = 0.0  # 0.0 to 1.0
    verified_claims: list[str] = field(default_factory=list)
    unverified_claims: list[str] = field(default_factory=list)
    remaining_work: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "progress_id": self.progress_id,
            "mission_id": self.mission_id,
            "goal_run_id": self.goal_run_id,
            "objective_coverage": self.objective_coverage,
            "verified_claims": list(self.verified_claims),
            "unverified_claims": list(self.unverified_claims),
            "remaining_work": list(self.remaining_work),
            "regressions": list(self.regressions),
            "confidence": self.confidence,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressAssessment:
        return cls(
            progress_id=str(data.get("progress_id") or f"prog_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            goal_run_id=str(data.get("goal_run_id") or "goal_run_1"),
            objective_coverage=float(data.get("objective_coverage", 0.0)),
            verified_claims=list(data.get("verified_claims") or []),
            unverified_claims=list(data.get("unverified_claims") or []),
            remaining_work=list(data.get("remaining_work") or []),
            regressions=list(data.get("regressions") or []),
            confidence=float(data.get("confidence", 1.0)),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class CompletionDecision:
    """Formal completion record verifying objective satisfaction (spec §14)."""

    completion_id: str = field(default_factory=lambda: f"comp_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    completion_reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    known_limitations: list[str] = field(default_factory=list)
    remaining_nonblocking_items: list[str] = field(default_factory=list)
    completed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "completion_id": self.completion_id,
            "mission_id": self.mission_id,
            "completion_reason": self.completion_reason,
            "evidence_refs": list(self.evidence_refs),
            "known_limitations": list(self.known_limitations),
            "remaining_nonblocking_items": list(self.remaining_nonblocking_items),
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompletionDecision:
        return cls(
            completion_id=str(data.get("completion_id") or f"comp_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            completion_reason=str(data.get("completion_reason") or ""),
            evidence_refs=list(data.get("evidence_refs") or []),
            known_limitations=list(data.get("known_limitations") or []),
            remaining_nonblocking_items=list(data.get("remaining_nonblocking_items") or []),
            completed_at=float(data.get("completed_at", time.time())),
        )


class WaitType(StrEnum):
    """Wait condition type (spec §17)."""

    TIME = "TIME"
    EVENT = "EVENT"
    USER_INPUT = "USER_INPUT"
    EXTERNAL_STATE = "EXTERNAL_STATE"
    RESOURCE_AVAILABLE = "RESOURCE_AVAILABLE"
    EXECUTION_COMPLETION = "EXECUTION_COMPLETION"
    APPROVAL = "APPROVAL"


@dataclass
class WaitCondition:
    """Durable wait condition (spec §17)."""

    condition_id: str = field(default_factory=lambda: f"wait_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    condition_type: WaitType = WaitType.EVENT
    predicate: str = ""
    expires_at: float | None = None
    wake_policy: str = "IMMEDIATE"
    status: str = "PENDING"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "mission_id": self.mission_id,
            "condition_type": str(self.condition_type),
            "predicate": self.predicate,
            "expires_at": self.expires_at,
            "wake_policy": self.wake_policy,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaitCondition:
        w_val = data.get("condition_type", "EVENT")
        try:
            condition_type = WaitType(str(w_val).upper())
        except ValueError:
            condition_type = WaitType.EVENT
        return cls(
            condition_id=str(data.get("condition_id") or f"wait_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            condition_type=condition_type,
            predicate=str(data.get("predicate") or ""),
            expires_at=float(data["expires_at"]) if data.get("expires_at") is not None else None,
            wake_policy=str(data.get("wake_policy") or "IMMEDIATE"),
            status=str(data.get("status") or "PENDING"),
            created_at=float(data.get("created_at", time.time())),
        )


class DependencyStatus(StrEnum):
    """External dependency status (spec §19)."""

    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ExternalDependency:
    """External dependency model (spec §19)."""

    dependency_id: str = field(default_factory=lambda: f"dep_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    kind: str = "APPROVAL"
    target: str = ""
    expected_state: str = "SATISFIED"
    current_state: str = "PENDING"
    last_checked_at: float = field(default_factory=time.time)
    next_check_at: float = field(default_factory=time.time)
    status: DependencyStatus = DependencyStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency_id": self.dependency_id,
            "mission_id": self.mission_id,
            "kind": self.kind,
            "target": self.target,
            "expected_state": self.expected_state,
            "current_state": self.current_state,
            "last_checked_at": self.last_checked_at,
            "next_check_at": self.next_check_at,
            "status": str(self.status),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExternalDependency:
        s_val = data.get("status", "PENDING")
        try:
            status = DependencyStatus(str(s_val).upper())
        except ValueError:
            status = DependencyStatus.PENDING
        return cls(
            dependency_id=str(data.get("dependency_id") or f"dep_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            kind=str(data.get("kind") or "APPROVAL"),
            target=str(data.get("target") or ""),
            expected_state=str(data.get("expected_state") or "SATISFIED"),
            current_state=str(data.get("current_state") or "PENDING"),
            last_checked_at=float(data.get("last_checked_at", time.time())),
            next_check_at=float(data.get("next_check_at", time.time())),
            status=status,
        )


class EscalationReason(StrEnum):
    """Reason for human escalation (spec §20)."""

    AMBIGUOUS_OBJECTIVE = "AMBIGUOUS_OBJECTIVE"
    RISK_THRESHOLD = "RISK_THRESHOLD"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    BUDGET_THRESHOLD = "BUDGET_THRESHOLD"
    REPEATED_FAILURE = "REPEATED_FAILURE"


@dataclass
class EscalationRequest:
    """Human escalation request model (spec §20)."""

    escalation_id: str = field(default_factory=lambda: f"esc_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    decision_id: str = ""
    reason: EscalationReason = EscalationReason.AMBIGUOUS_OBJECTIVE
    question: str = ""
    context_summary: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    allowed_responses: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: str = "PENDING"
    reply: str | None = None
    replied_at: float | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "RESOLVED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "escalation_id": self.escalation_id,
            "mission_id": self.mission_id,
            "decision_id": self.decision_id,
            "reason": str(self.reason),
            "question": self.question,
            "context_summary": self.context_summary,
            "evidence_refs": list(self.evidence_refs),
            "allowed_responses": list(self.allowed_responses),
            "created_at": self.created_at,
            "status": self.status,
            "reply": self.reply,
            "replied_at": self.replied_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EscalationRequest:
        r_val = data.get("reason", "AMBIGUOUS_OBJECTIVE")
        try:
            reason = EscalationReason(str(r_val).upper())
        except ValueError:
            reason = EscalationReason.AMBIGUOUS_OBJECTIVE
        return cls(
            escalation_id=str(data.get("escalation_id") or f"esc_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            decision_id=str(data.get("decision_id") or ""),
            reason=reason,
            question=str(data.get("question") or ""),
            context_summary=str(data.get("context_summary") or ""),
            evidence_refs=list(data.get("evidence_refs") or []),
            allowed_responses=list(data.get("allowed_responses") or []),
            created_at=float(data.get("created_at", time.time())),
            status=str(data.get("status") or "PENDING"),
            reply=data.get("reply"),
            replied_at=float(data["replied_at"]) if data.get("replied_at") is not None else None,
        )


class InterruptCategory(StrEnum):
    """Category of user/external interrupt (spec §21)."""

    CLARIFICATION = "CLARIFICATION"
    OBJECTIVE_CHANGE = "OBJECTIVE_CHANGE"
    CONSTRAINT_CHANGE = "CONSTRAINT_CHANGE"
    CANCEL = "CANCEL"
    PRIORITY_CHANGE = "PRIORITY_CHANGE"
    ADDITIONAL_CONTEXT = "ADDITIONAL_CONTEXT"


@dataclass
class MissionRevision:
    """Mission objective or constraint revision record (spec §22)."""

    revision_id: str = field(default_factory=lambda: f"rev_{uuid.uuid4().hex[:12]}")
    mission_id: str = "default"
    parent_revision_id: str | None = None
    previous_objective: str = ""
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    reason: str = ""
    source: str = "USER"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "mission_id": self.mission_id,
            "parent_revision_id": self.parent_revision_id,
            "previous_objective": self.previous_objective,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "reason": self.reason,
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MissionRevision:
        return cls(
            revision_id=str(data.get("revision_id") or f"rev_{uuid.uuid4().hex[:12]}"),
            mission_id=str(data.get("mission_id") or "default"),
            parent_revision_id=data.get("parent_revision_id"),
            previous_objective=str(data.get("previous_objective") or ""),
            objective=str(data.get("objective") or ""),
            constraints=list(data.get("constraints") or []),
            reason=str(data.get("reason") or ""),
            source=str(data.get("source") or "USER"),
            created_at=float(data.get("created_at", time.time())),
        )


class RiskLevel(StrEnum):
    """Autonomous risk gate levels (spec §27)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    REQUIRES_OWNER = "REQUIRES_OWNER"


@dataclass
class BudgetState:
    """Autonomous budget state tracking (spec §26)."""

    remaining_compute: float = 100.0
    remaining_execution: int = 50
    remaining_wall_time_s: float = 3600.0
    retry_count: int = 0
    provider_availability: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "remaining_compute": self.remaining_compute,
            "remaining_execution": self.remaining_execution,
            "remaining_wall_time_s": self.remaining_wall_time_s,
            "retry_count": self.retry_count,
            "provider_availability": dict(self.provider_availability),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetState:
        return cls(
            remaining_compute=float(data.get("remaining_compute", 100.0)),
            remaining_execution=int(data.get("remaining_execution", 50)),
            remaining_wall_time_s=float(data.get("remaining_wall_time_s", 3600.0)),
            retry_count=int(data.get("retry_count", 0)),
            provider_availability=dict(data.get("provider_availability") or {}),
        )


@dataclass
class AutonomousState:
    """MasterAgent current autonomous cognitive stage (spec §3)."""

    mission_id: str
    goal_run_id: str
    cycle_id: str = "cycle_1"
    state: AutonomousStatus = AutonomousStatus.OBSERVING
    objective: str = ""
    current_hypothesis: str = ""
    accepted_progress: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    blocking_conditions: list[str] = field(default_factory=list)
    pending_observations: list[str] = field(default_factory=list)
    next_action: str | None = None
    wake_condition: str | None = None
    last_decision_id: str | None = None
    last_evaluation_id: str | None = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "goal_run_id": self.goal_run_id,
            "cycle_id": self.cycle_id,
            "state": str(self.state),
            "objective": self.objective,
            "current_hypothesis": self.current_hypothesis,
            "accepted_progress": list(self.accepted_progress),
            "open_questions": list(self.open_questions),
            "blocking_conditions": list(self.blocking_conditions),
            "pending_observations": list(self.pending_observations),
            "next_action": self.next_action,
            "wake_condition": self.wake_condition,
            "last_decision_id": self.last_decision_id,
            "last_evaluation_id": self.last_evaluation_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutonomousState:
        st_val = data.get("state", "OBSERVING")
        try:
            state = AutonomousStatus(str(st_val).upper())
        except ValueError:
            state = AutonomousStatus.OBSERVING
        return cls(
            mission_id=str(data["mission_id"]),
            goal_run_id=str(data["goal_run_id"]),
            cycle_id=str(data.get("cycle_id", "cycle_1")),
            state=state,
            objective=str(data.get("objective", "")),
            current_hypothesis=str(data.get("current_hypothesis", "")),
            accepted_progress=list(data.get("accepted_progress") or []),
            open_questions=list(data.get("open_questions") or []),
            blocking_conditions=list(data.get("blocking_conditions") or []),
            pending_observations=list(data.get("pending_observations") or []),
            next_action=data.get("next_action"),
            wake_condition=data.get("wake_condition"),
            last_decision_id=data.get("last_decision_id"),
            last_evaluation_id=data.get("last_evaluation_id"),
            updated_at=float(data.get("updated_at", time.time())),
        )
