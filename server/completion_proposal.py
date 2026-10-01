"""P0-08 Completion Verification Contract — named pipeline objects."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class CriterionType(StrEnum):
    TEST = "TEST"
    BUILD = "BUILD"
    STATIC = "STATIC"
    RUNTIME = "RUNTIME"
    PRODUCT_PROBE = "PRODUCT_PROBE"
    ARTIFACT = "ARTIFACT"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CUSTOM = "CUSTOM"


class CompletionDecisionValue(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    NEEDS_MORE_WORK = "NEEDS_MORE_WORK"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class AcceptanceCriteria:
    """Machine-verifiable acceptance criteria for a goal."""

    criteria_id: str
    goal_run_id: str
    checks: list[CriterionType] = field(default_factory=list)
    custom_checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criteria_id": self.criteria_id,
            "goal_run_id": self.goal_run_id,
            "checks": [c.value for c in self.checks],
            "custom_checks": self.custom_checks,
        }


@dataclass(frozen=True)
class CompletionProposal:
    """A proposal that a goal is complete. Zero authority without evidence."""

    proposal_id: str
    goal_run_id: str
    agent_id: str = ""
    summary: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationPlan:
    """Plan generated from Goal acceptance criteria, not Agent preference."""

    verification_id: str
    goal_run_id: str
    checks: list[CriterionType] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "goal_run_id": self.goal_run_id,
            "checks": [c.value for c in self.checks],
            "required_evidence": self.required_evidence,
        }


@dataclass(frozen=True)
class VerificationEvidence:
    """Evidence for a verification check."""

    evidence_id: str
    check: CriterionType
    result: str
    source: str
    execution_id: str | None = None
    artifact_id: str | None = None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["check"] = self.check.value
        return data


@dataclass(frozen=True)
class CompletionDecision:
    """Final completion decision. Budget exhaustion is never ACCEPT."""

    decision_id: str
    goal_run_id: str
    outcome: CompletionDecisionValue
    reason: str = ""
    evidence_ids: tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outcome"] = self.outcome.value
        return data


def new_proposal(goal_run_id: str, *, agent_id: str = "") -> CompletionProposal:
    return CompletionProposal(
        proposal_id=str(uuid.uuid4()),
        goal_run_id=goal_run_id,
        agent_id=agent_id,
    )


def new_verification_plan(
    goal_run_id: str,
    criteria: AcceptanceCriteria,
) -> VerificationPlan:
    return VerificationPlan(
        verification_id=str(uuid.uuid4()),
        goal_run_id=goal_run_id,
        checks=list(criteria.checks),
        required_evidence=[f"evidence:{c.value}" for c in criteria.checks],
    )


def new_evidence(
    check: CriterionType,
    result: str,
    source: str,
    *,
    execution_id: str | None = None,
) -> VerificationEvidence:
    return VerificationEvidence(
        evidence_id=str(uuid.uuid4()),
        check=check,
        result=result,
        source=source,
        execution_id=execution_id,
    )


def decide(
    goal_run_id: str,
    *,
    all_passed: bool,
    budget_exhausted: bool = False,
    blockers: list[str] | None = None,
) -> CompletionDecision:
    """Make a completion decision. Budget exhaustion is never ACCEPT."""
    if budget_exhausted:
        return CompletionDecision(
            decision_id=str(uuid.uuid4()),
            goal_run_id=goal_run_id,
            outcome=CompletionDecisionValue.BLOCKED,
            reason="budget exhausted",
        )
    if blockers:
        return CompletionDecision(
            decision_id=str(uuid.uuid4()),
            goal_run_id=goal_run_id,
            outcome=CompletionDecisionValue.BLOCKED,
            reason=f"blockers: {', '.join(blockers)}",
        )
    if all_passed:
        return CompletionDecision(
            decision_id=str(uuid.uuid4()),
            goal_run_id=goal_run_id,
            outcome=CompletionDecisionValue.ACCEPT,
            reason="all checks passed",
        )
    return CompletionDecision(
        decision_id=str(uuid.uuid4()),
        goal_run_id=goal_run_id,
        outcome=CompletionDecisionValue.NEEDS_MORE_WORK,
        reason="not all checks passed",
    )
