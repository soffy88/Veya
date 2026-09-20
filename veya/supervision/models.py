"""Canonical contracts for the Veya Dual/Auto Supervision Runtime.

This module defines *only* the shared vocabulary: one Mission, one status
machine, one ExecutionReport, one SupervisorReview, one lineage record. It adds
no second runtime — Mission wraps the existing GoalRun/MasterAgent execution
authorities, and no mode-specific state machine may be introduced elsewhere.

Nothing here performs I/O or executes side effects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SupervisionMode(StrEnum):
    """Who owns design/review authority for a Mission."""

    external = "external"  # ChatGPT supervisor
    internal = "internal"  # Veya InternalSupervisor
    auto = "auto"  # SupervisionRouter decides, and may switch at runtime


class MissionStatus(StrEnum):
    """THE canonical state machine; every mode uses exactly this one."""

    created = "CREATED"
    routing_supervisor = "ROUTING_SUPERVISOR"
    designing = "DESIGNING"
    planning = "PLANNING"
    executing = "EXECUTING"
    collecting_evidence = "COLLECTING_EVIDENCE"
    fast_decision = "FAST_DECISION"
    reviewing = "REVIEWING"
    retasking = "RETASKING"
    accepted = "ACCEPTED"
    done = "DONE"
    # exceptional
    blocked = "BLOCKED"
    waiting_external_supervisor = "WAITING_EXTERNAL_SUPERVISOR"
    waiting_owner = "WAITING_OWNER"
    failed = "FAILED"
    cancelled = "CANCELLED"


TERMINAL_STATUSES: frozenset[MissionStatus] = frozenset(
    {MissionStatus.done, MissionStatus.failed, MissionStatus.cancelled}
)


class ReviewDecision(StrEnum):
    """Runtime must never parse free text for an action; it reads this."""

    accept = "ACCEPT"
    continue_ = "CONTINUE"
    revise = "REVISE"
    retry = "RETRY"
    rollback = "ROLLBACK"
    escalate = "ESCALATE"
    done = "DONE"


class EscalationCode(StrEnum):
    """The ONLY reasons a mission may interrupt the owner (spec §23)."""

    owner_credential_required = "OWNER_CREDENTIAL_REQUIRED"
    irreversible_external_action = "IRREVERSIBLE_EXTERNAL_ACTION"
    legal_policy_confirmation_required = "LEGAL_POLICY_CONFIRMATION_REQUIRED"
    production_destructive_action = "PRODUCTION_DESTRUCTIVE_ACTION"
    unresolvable_authority_conflict = "UNRESOLVABLE_AUTHORITY_CONFLICT"
    resource_owner_input_required = "RESOURCE_OWNER_INPUT_REQUIRED"


class SideEffectClass(StrEnum):
    """Class carried by every ExecutionTask for planner/supervisor decisions."""

    read = "read"
    write = "write"
    destructive = "destructive"
    external = "external"


class ExecutorKind(StrEnum):
    """Concrete executor a planner may choose (spec §17/§18)."""

    hicode = "hicode"  # coding / repo modification / tests / refactor
    dsh = "dsh"  # shell-heavy / ops / diagnostics / environment
    worker = "worker"
    native_tool = "native_tool"


@dataclass
class ExecutionTask:
    """Canonical planner → executor task (spec §17)."""

    task_id: str
    objective: str
    executor: ExecutorKind = ExecutorKind.hicode
    inputs: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    timeout_s: float | None = None
    side_effect_class: SideEffectClass = SideEffectClass.read

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "executor": str(self.executor),
            "inputs": dict(self.inputs),
            "constraints": list(self.constraints),
            "acceptance": list(self.acceptance),
            "timeout_s": self.timeout_s,
            "side_effect_class": str(self.side_effect_class),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionTask:
        return cls(
            task_id=str(data["task_id"]),
            objective=str(data["objective"]),
            executor=ExecutorKind(data.get("executor", "hicode")),
            inputs=dict(data.get("inputs") or {}),
            constraints=[str(c) for c in data.get("constraints") or []],
            acceptance=[str(a) for a in data.get("acceptance") or []],
            timeout_s=data.get("timeout_s"),
            side_effect_class=SideEffectClass(data.get("side_effect_class", "read")),
        )


@dataclass
class MissionBudget:
    max_iterations: int = 10
    max_runtime_s: float | None = None
    max_external_reviews: int | None = None
    max_jev_calls: int | None = None
    cost_policy: str = "balanced"
    latency_policy: str = "balanced"

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_iterations": self.max_iterations,
            "max_runtime_s": self.max_runtime_s,
            "max_external_reviews": self.max_external_reviews,
            "max_jev_calls": self.max_jev_calls,
            "cost_policy": self.cost_policy,
            "latency_policy": self.latency_policy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MissionBudget:
        data = data or {}
        return cls(
            max_iterations=int(data.get("max_iterations", 10)),
            max_runtime_s=data.get("max_runtime_s"),
            max_external_reviews=data.get("max_external_reviews"),
            max_jev_calls=data.get("max_jev_calls"),
            cost_policy=str(data.get("cost_policy", "balanced")),
            latency_policy=str(data.get("latency_policy", "balanced")),
        )


@dataclass
class MissionPolicies:
    supervisor_policy: dict[str, Any] = field(default_factory=dict)
    execution_policy: dict[str, Any] = field(default_factory=dict)
    review_policy: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supervisor_policy": dict(self.supervisor_policy),
            "execution_policy": dict(self.execution_policy),
            "review_policy": dict(self.review_policy),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MissionPolicies:
        data = data or {}
        return cls(
            supervisor_policy=dict(data.get("supervisor_policy") or {}),
            execution_policy=dict(data.get("execution_policy") or {}),
            review_policy=dict(data.get("review_policy") or {}),
        )


@dataclass
class Mission:
    """Canonical mission record (spec §4)."""

    mission_id: str
    goal: str
    supervision_mode: SupervisionMode = SupervisionMode.auto
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    workspace: str = ""
    authority: dict[str, Any] = field(default_factory=dict)
    autonomy: dict[str, Any] = field(default_factory=dict)
    budget: MissionBudget = field(default_factory=MissionBudget)
    deadline: float | None = None
    priority: str = "normal"
    policies: MissionPolicies = field(default_factory=MissionPolicies)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    status: MissionStatus = MissionStatus.created

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "goal": self.goal,
            "supervision_mode": str(self.supervision_mode),
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "workspace": self.workspace,
            "authority": dict(self.authority),
            "autonomy": dict(self.autonomy),
            "budget": self.budget.to_dict(),
            "deadline": self.deadline,
            "priority": self.priority,
            "policies": self.policies.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": str(self.status),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mission:
        return cls(
            mission_id=str(data["mission_id"]),
            goal=str(data["goal"]),
            supervision_mode=SupervisionMode(data.get("supervision_mode", "auto")),
            constraints=[str(c) for c in data.get("constraints") or []],
            acceptance_criteria=[str(c) for c in data.get("acceptance_criteria") or []],
            workspace=str(data.get("workspace", "")),
            authority=dict(data.get("authority") or {}),
            autonomy=dict(data.get("autonomy") or {}),
            budget=MissionBudget.from_dict(data.get("budget")),
            deadline=data.get("deadline"),
            priority=str(data.get("priority", "normal")),
            policies=MissionPolicies.from_dict(data.get("policies")),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            status=MissionStatus(data.get("status", "CREATED")),
        )


@dataclass
class ExecutionReport:
    """Canonical per-iteration report (spec §6). Raw logs stay in artifacts."""

    mission_id: str
    iteration: int
    objective: str
    status: str
    goalrun_id: str | None = None
    checkpoint_id: str | None = None
    changes: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    runtime_evidence: list[dict[str, Any]] = field(default_factory=list)
    git_diff_summary: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    unresolved_risks: list[dict[str, Any]] = field(default_factory=list)
    deviations: list[dict[str, Any]] = field(default_factory=list)
    blocked_items: list[dict[str, Any]] = field(default_factory=list)
    jev_decisions: list[dict[str, Any]] = field(default_factory=list)
    executor_summary: str = ""
    proposed_next_action: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "goalrun_id": self.goalrun_id,
            "iteration": self.iteration,
            "checkpoint_id": self.checkpoint_id,
            "objective": self.objective,
            "status": self.status,
            "changes": list(self.changes),
            "tests": list(self.tests),
            "artifacts": list(self.artifacts),
            "runtime_evidence": list(self.runtime_evidence),
            "git_diff_summary": dict(self.git_diff_summary),
            "failures": list(self.failures),
            "unresolved_risks": list(self.unresolved_risks),
            "deviations": list(self.deviations),
            "blocked_items": list(self.blocked_items),
            "jev_decisions": list(self.jev_decisions),
            "executor_summary": self.executor_summary,
            "proposed_next_action": self.proposed_next_action,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionReport:
        return cls(
            mission_id=str(data["mission_id"]),
            iteration=int(data.get("iteration", 0)),
            objective=str(data.get("objective", "")),
            status=str(data.get("status", "")),
            goalrun_id=data.get("goalrun_id"),
            checkpoint_id=data.get("checkpoint_id"),
            changes=list(data.get("changes") or []),
            tests=list(data.get("tests") or []),
            artifacts=list(data.get("artifacts") or []),
            runtime_evidence=list(data.get("runtime_evidence") or []),
            git_diff_summary=dict(data.get("git_diff_summary") or {}),
            failures=list(data.get("failures") or []),
            unresolved_risks=list(data.get("unresolved_risks") or []),
            deviations=list(data.get("deviations") or []),
            blocked_items=list(data.get("blocked_items") or []),
            jev_decisions=list(data.get("jev_decisions") or []),
            executor_summary=str(data.get("executor_summary", "")),
            proposed_next_action=data.get("proposed_next_action"),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class SupervisorReview:
    """Canonical review output; identical for external and internal (spec §7)."""

    mission_id: str
    iteration: int
    supervisor: str  # "external" | "internal"
    decision: ReviewDecision
    reason: str = ""
    next_task: str | None = None
    constraints_delta: list[str] = field(default_factory=list)
    acceptance_delta: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    confidence: float | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "iteration": self.iteration,
            "supervisor": self.supervisor,
            "decision": str(self.decision),
            "reason": self.reason,
            "next_task": self.next_task,
            "constraints_delta": list(self.constraints_delta),
            "acceptance_delta": list(self.acceptance_delta),
            "required_evidence": list(self.required_evidence),
            "risk_notes": list(self.risk_notes),
            "confidence": self.confidence,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SupervisorReview:
        return cls(
            mission_id=str(data["mission_id"]),
            iteration=int(data.get("iteration", 0)),
            supervisor=str(data.get("supervisor", "")),
            decision=ReviewDecision(data["decision"]),
            reason=str(data.get("reason", "")),
            next_task=data.get("next_task"),
            constraints_delta=[str(x) for x in data.get("constraints_delta") or []],
            acceptance_delta=[str(x) for x in data.get("acceptance_delta") or []],
            required_evidence=[str(x) for x in data.get("required_evidence") or []],
            risk_notes=[str(x) for x in data.get("risk_notes") or []],
            confidence=data.get("confidence"),
            created_at=float(data.get("created_at", time.time())),
        )


@dataclass
class LineageEntry:
    """One supervisor switch (spec §14)."""

    iteration: int
    from_supervisor: str | None
    to_supervisor: str
    reason: str
    trigger: str
    confidence: float | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "from": self.from_supervisor,
            "to": self.to_supervisor,
            "reason": self.reason,
            "trigger": self.trigger,
            "confidence": self.confidence,
            "timestamp": self.timestamp,
        }
