"""Autonomous Planner Adapter (spec §30, §31, §38).

Invariants:
- MasterAgent remains the sole semantic authority (SECOND_PLANNER_AUTHORITY=0).
- Planner only produces PlanProposal; it cannot own Mission or decide completion.
- MasterAgent explicitly accepts, modifies, or rejects proposals before GoalRun update.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .models import PlanProposal


class AutonomousPlannerAdapter:
    """Adapter wrapping planner decomposition into PlanProposal (spec §30, §31)."""

    def __init__(self, planner_backend: Any = None) -> None:
        self._planner = planner_backend

    def propose_plan(
        self,
        mission_id: str,
        objective: str,
        revision_id: str = "rev_1",
        assumptions: list[str] | None = None,
        goals: list[dict[str, Any]] | None = None,
        dependencies: dict[str, list[str]] | None = None,
        acceptance_criteria: list[str] | None = None,
        verification_strategy: str = "",
        risk_notes: list[str] | None = None,
    ) -> PlanProposal:
        """Produce candidate PlanProposal for MasterAgent evaluation."""
        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        default_goals = goals or [
            {
                "goal_id": f"{plan_id}_g1",
                "description": f"Initial subtask for: {objective}",
            }
        ]
        default_acceptance = acceptance_criteria or [
            f"Verification evidence produced for: {objective}"
        ]

        return PlanProposal(
            plan_id=plan_id,
            mission_id=mission_id,
            revision_id=revision_id,
            assumptions=list(assumptions or ["Environment healthy", "Tools available"]),
            goals=list(default_goals),
            dependencies=dict(dependencies or {}),
            acceptance_criteria=list(default_acceptance),
            verification_strategy=verification_strategy
            or "Check durable effect receipt and evidence",
            risk_notes=list(risk_notes or []),
            created_at=time.time(),
        )

    def evaluate_proposal(
        self,
        proposal: PlanProposal,
        context: Any = None,
    ) -> tuple[bool, str]:
        """Validate proposal structure and constraints without semantic authority."""
        if not proposal.goals:
            return False, "PROPOSAL_REJECTED: no executable goals declared"
        if not proposal.acceptance_criteria:
            return False, "PROPOSAL_REJECTED: no acceptance criteria defined"
        return True, "PROPOSAL_VALID"

    def accept_proposal(
        self,
        proposal: PlanProposal,
        modifications: dict[str, Any] | None = None,
    ) -> PlanProposal:
        """MasterAgent accepts (and optionally modifies) proposal."""
        if not modifications:
            return proposal

        # Apply modifications under MasterAgent authority
        modified_goals = modifications.get("goals", proposal.goals)
        modified_assumptions = modifications.get("assumptions", proposal.assumptions)
        modified_deps = modifications.get("dependencies", proposal.dependencies)
        modified_criteria = modifications.get("acceptance_criteria", proposal.acceptance_criteria)

        return PlanProposal(
            plan_id=proposal.plan_id,
            mission_id=proposal.mission_id,
            revision_id=proposal.revision_id,
            assumptions=list(modified_assumptions),
            goals=list(modified_goals),
            dependencies=dict(modified_deps),
            acceptance_criteria=list(modified_criteria),
            verification_strategy=str(
                modifications.get("verification_strategy", proposal.verification_strategy)
            ),
            risk_notes=list(modifications.get("risk_notes", proposal.risk_notes)),
            created_at=proposal.created_at,
        )
