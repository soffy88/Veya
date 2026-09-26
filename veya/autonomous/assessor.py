"""Situation Assessment under MasterAgent semantic authority (spec §6).

Invariants:
- Generated under MasterAgent authority only.
- Filters out STALE and CONFLICTING observations from known_facts.
- Identifies blockers, uncertainties, risks, candidate next moves.
"""

from __future__ import annotations

import time
from typing import Any

from .models import (
    BudgetState,
    Observation,
    ObservationStatus,
    SituationAssessment,
)


class SituationAssessor:
    """MasterAgent helper for producing situation assessments (spec §6)."""

    def assess(
        self,
        mission_id: str,
        cycle_id: str,
        objective: str = "",
        observations: list[Observation] | None = None,
        budget: BudgetState | None = None,
        blocking_conditions: list[str] | None = None,
        constraints: list[str] | None = None,
        active_hypothesis: str = "",
        reconciled_context: Any = None,
        active_goals: list[str] | None = None,
    ) -> SituationAssessment:
        known_facts: list[str] = []
        uncertainties: list[str] = []
        risks: list[str] = []
        blockers = list(blocking_conditions or [])
        cons = list(constraints or [])

        obs_list = list(observations or [])
        if reconciled_context is not None:
            if hasattr(reconciled_context, "current"):
                obs_list.extend(reconciled_context.current)
                obs_list.extend(reconciled_context.conflicting)
                obs_list.extend(reconciled_context.unverified)
                obs_list.extend(reconciled_context.stale)
            elif isinstance(reconciled_context, (list, tuple)):
                obs_list.extend(reconciled_context)

        # Process observations based on reconciled status
        for obs in obs_list:
            if obs.status == ObservationStatus.CURRENT:
                if obs.kind == "BLOCKER":
                    blockers.append(obs.summary)
                elif obs.kind == "RISK":
                    risks.append(obs.summary)
                else:
                    known_facts.append(obs.summary)
            elif obs.status == ObservationStatus.CONFLICTING:
                uncertainties.append(f"CONFLICT: {obs.summary}")
            elif obs.status == ObservationStatus.UNVERIFIED:
                uncertainties.append(f"UNVERIFIED: {obs.summary}")
            # STALE observations are excluded from known_facts

        # Check budget risks
        if budget is not None:
            if budget.remaining_wall_time_s < 300:
                risks.append("WALL_TIME_BUDGET_LOW")
            if budget.remaining_execution <= 2:
                risks.append("EXECUTION_BUDGET_LOW")
            if budget.retry_count >= 3:
                risks.append("REPEATED_FAILURES_DETECTED")

        # Determine progress state
        progress_state = "IN_PROGRESS"
        if blockers:
            progress_state = "BLOCKED"

        # Candidate next moves
        candidate_moves: list[str] = []
        if blockers:
            candidate_moves.append("RESOLVE_BLOCKER")
            candidate_moves.append("ESCALATE_TO_OWNER")
        else:
            candidate_moves.append("EXECUTE_NEXT_SUBTASK")
            candidate_moves.append("VERIFY_PROGRESS")

        return SituationAssessment(
            mission_id=mission_id,
            cycle_id=cycle_id,
            known_facts=known_facts,
            uncertainties=uncertainties,
            constraints=cons,
            risks=risks,
            blocking_conditions=blockers,
            progress_state=progress_state,
            candidate_next_moves=candidate_moves,
            created_at=time.time(),
        )
