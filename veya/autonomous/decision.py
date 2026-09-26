"""Decision Records, Preconditions, and Action Proposals (spec §7, §8, §25, §28).

Invariants:
- All semantic decisions produced under MasterAgent authority.
- Decision preconditions must be satisfied before ACT / REPLAN / COMPLETE.
- Failures result in WAIT, ESCALATE, or ABORT without silent fallback.
- Decisions are durably persisted and link evidence refs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    AutonomousDecision,
    BudgetState,
    DecisionType,
    EscalationReason,
)


@dataclass
class PreconditionCheckResult:
    ok: bool
    recommended_decision: DecisionType | None = None
    reason: str = ""
    escalation_reason: EscalationReason | None = None


class DecisionPreconditions:
    """Validate prerequisites before committing to ACT, REPLAN, or COMPLETE (spec §8)."""

    def check_preconditions(
        self,
        decision_type: DecisionType,
        objective: str = "",
        objective_valid: bool = True,
        has_blocking_interrupt: bool = False,
        blocking_conditions: list[str] | None = None,
        workspace_valid: bool = True,
        security_policy_satisfied: bool = True,
        capabilities_available: bool = True,
        budget_available: bool = True,
        required_evidence_available: bool = True,
        **kwargs: Any,
    ) -> tuple[bool, list[str]]:
        """Validate prerequisites before committing (spec §8)."""
        violations: list[str] = []
        if not objective_valid:
            violations.append("OBJECTIVE_INVALID")
        if has_blocking_interrupt:
            violations.append("BLOCKING_INTERRUPT")
        if blocking_conditions and decision_type != DecisionType.WAIT:
            violations.append(f"BLOCKING_CONDITIONS: {blocking_conditions}")
        if not workspace_valid:
            violations.append("WORKSPACE_INVALID")
        if not security_policy_satisfied:
            violations.append("SECURITY_POLICY_VIOLATION")
        if not capabilities_available:
            violations.append("CAPABILITIES_UNAVAILABLE")
        if not budget_available:
            violations.append("BUDGET_EXHAUSTED")
        if decision_type == DecisionType.COMPLETE and not required_evidence_available:
            violations.append("REQUIRED_EVIDENCE_UNAVAILABLE")

        return len(violations) == 0, violations

    def check(
        self,
        decision_type: DecisionType,
        objective: str,
        budget: BudgetState | None = None,
        workspace_valid: bool = True,
        security_policy_satisfied: bool = True,
        unresolved_interrupts: list[str] | None = None,
        blocking_conditions: list[str] | None = None,
        evidence_present: bool = True,
        runtime_capabilities_available: bool = True,
    ) -> PreconditionCheckResult:
        # 1. Objective validity
        if not objective or not objective.strip():
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.ESCALATE,
                reason="OBJECTIVE_INVALID_OR_EMPTY",
                escalation_reason=EscalationReason.AMBIGUOUS_OBJECTIVE,
            )

        # 2. Unresolved blocking interrupts
        if unresolved_interrupts:
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.WAIT,
                reason=f"UNRESOLVED_INTERRUPT: {unresolved_interrupts[0]}",
            )

        # 3. Blocking conditions
        if blocking_conditions:
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.WAIT,
                reason=f"BLOCKING_CONDITION: {blocking_conditions[0]}",
            )

        # 4. Workspace validity
        if not workspace_valid:
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.ABORT,
                reason="WORKSPACE_INVALID_OR_DENIED",
            )

        # 5. Security policy
        if not security_policy_satisfied:
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.ESCALATE,
                reason="SECURITY_POLICY_VIOLATION",
                escalation_reason=EscalationReason.PERMISSION_REQUIRED,
            )

        # 6. Runtime capabilities
        if not runtime_capabilities_available:
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.WAIT,
                reason="RUNTIME_CAPABILITY_UNAVAILABLE",
            )

        # 7. Budget checks
        if budget is not None and (
            budget.remaining_execution <= 0 or budget.remaining_wall_time_s <= 0
        ):
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.ESCALATE,
                reason="BUDGET_EXHAUSTED",
                escalation_reason=EscalationReason.BUDGET_THRESHOLD,
            )

        # 8. Evidence requirements for COMPLETE
        if decision_type == DecisionType.COMPLETE and not evidence_present:
            return PreconditionCheckResult(
                ok=False,
                recommended_decision=DecisionType.ACT,
                reason="UNVERIFIED_COMPLETION_PREVENTED: required evidence missing",
            )

        return PreconditionCheckResult(ok=True)


class DecisionStore:
    """Durable store for AutonomousDecision records (spec §7)."""

    def __init__(self, persistence_path: str | Path | None = None):
        self._path = Path(persistence_path) if persistence_path else None
        self._decisions: list[AutonomousDecision] = []
        self._by_id: dict[str, AutonomousDecision] = {}
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        if not self._path or not self._path.is_file():
            return
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    dec = AutonomousDecision.from_dict(data)
                    self._decisions.append(dec)
                    self._by_id[dec.decision_id] = dec
                except Exception:
                    continue

    def save(self, decision: AutonomousDecision) -> AutonomousDecision:
        self._decisions.append(decision)
        self._by_id[decision.decision_id] = decision
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")
        return decision

    def get(self, decision_id: str) -> AutonomousDecision | None:
        return self._by_id.get(decision_id)

    def append(self, decision: AutonomousDecision) -> AutonomousDecision:
        return self.save(decision)

    def get_decision(self, decision_id: str) -> AutonomousDecision | None:
        return self.get(decision_id)

    def query(self, mission_id: str, limit: int = 100) -> list[AutonomousDecision]:
        return [d for d in self._decisions if d.mission_id == mission_id][-limit:]

    def list_for_mission(self, mission_id: str) -> list[AutonomousDecision]:
        return [d for d in self._decisions if d.mission_id == mission_id]

    def count(self, mission_id: str | None = None) -> int:
        if mission_id is None:
            return len(self._decisions)
        return sum(1 for d in self._decisions if d.mission_id == mission_id)
