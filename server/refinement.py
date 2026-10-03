"""P1-04/P1-05 Continual Harness — evidence-backed refinement with evaluation gate.

Refinement candidates are promoted only through:
  Evidence → Candidate → EvaluationSuite → Regression comparison → Qualification → Snapshot → Promotion

Forbidden: trajectory → immediate global prompt rewrite, model decides its own
security policy, model promotes untested tool permission, model rewrites immutable authority.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RefinementTarget(StrEnum):
    SKILL = "SKILL"
    CONTEXT_POLICY = "CONTEXT_POLICY"
    ROUTING_POLICY = "ROUTING_POLICY"
    WORKFLOW = "WORKFLOW"
    AGENT_SPEC = "AGENT_SPEC"
    SUPPLEMENTAL_INSTRUCTION = "SUPPLEMENTAL_INSTRUCTION"


class RiskClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RefinementStatus(StrEnum):
    PROPOSED = "PROPOSED"
    EVALUATING = "EVALUATING"
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"
    PROMOTED = "PROMOTED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class RefinementCandidate:
    """A proposed harness refinement with evidence."""

    candidate_id: str
    source_runs: tuple[str, ...]
    observed_problem: str
    evidence: tuple[str, ...]
    target_type: RefinementTarget
    target_id: str
    proposed_change: str
    risk_class: RiskClass
    status: RefinementStatus = RefinementStatus.PROPOSED
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target_type"] = self.target_type.value
        data["risk_class"] = self.risk_class.value
        data["status"] = self.status.value
        return data


@dataclass(frozen=True)
class RefinementSnapshot:
    """A qualified snapshot ready for promotion."""

    snapshot_id: str
    candidate_id: str
    target_type: RefinementTarget
    target_id: str
    change: str
    evaluation_run_id: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target_type"] = self.target_type.value
        return data


def new_candidate(
    *,
    source_runs: list[str],
    observed_problem: str,
    evidence: list[str],
    target_type: RefinementTarget,
    target_id: str,
    proposed_change: str,
    risk_class: RiskClass,
) -> RefinementCandidate:
    """Create a new refinement candidate."""
    return RefinementCandidate(
        candidate_id=str(uuid.uuid4()),
        source_runs=tuple(source_runs),
        observed_problem=observed_problem,
        evidence=tuple(evidence),
        target_type=target_type,
        target_id=target_id,
        proposed_change=proposed_change,
        risk_class=risk_class,
    )


def is_immutable_target(target_type: RefinementTarget) -> bool:
    """Check if a target type is immutable (cannot be rewritten by refinement)."""
    return target_type in {
        RefinementTarget.ROUTING_POLICY,
    }
