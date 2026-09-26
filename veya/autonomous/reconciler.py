"""Goal Reconciliation, Replan, Retask, and Mission Revision (spec §9, §10, §11, §22).

Invariants:
- GoalRun remains the sole durable execution authority (SECOND_GOAL_ENGINE=0).
- Accepted progress is strictly preserved; no full reset by default (ACCEPTED_PROGRESS_LOST=0).
- Retask modifies only single subtask execution parameters without altering top-level objective.
- Replan preserves completed steps, invalidates obsolete assumptions, updates future graph.
- Mission revision preserves accepted progress, creates explicit MissionRevision record.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .models import MissionRevision


@dataclass
class ReplanResult:
    replan_id: str
    decision_id: str
    preserved_progress: list[str]
    invalidated_assumptions: list[str]
    cancelled_future_steps: list[str]
    new_goal_graph: list[dict[str, Any]]
    verification_requirements: list[str]
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "replan_id": self.replan_id,
            "decision_id": self.decision_id,
            "preserved_progress": list(self.preserved_progress),
            "invalidated_assumptions": list(self.invalidated_assumptions),
            "cancelled_future_steps": list(self.cancelled_future_steps),
            "new_goal_graph": [dict(g) for g in self.new_goal_graph],
            "verification_requirements": list(self.verification_requirements),
            "created_at": self.created_at,
        }


@dataclass
class RetaskResult:
    retask_id: str
    decision_id: str
    subtask_id: str
    previous_attempt: dict[str, Any]
    new_attempt: dict[str, Any]
    retask_reason: str
    expected_difference: str
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "retask_id": self.retask_id,
            "decision_id": self.decision_id,
            "subtask_id": self.subtask_id,
            "previous_attempt": dict(self.previous_attempt),
            "new_attempt": dict(self.new_attempt),
            "retask_reason": self.retask_reason,
            "expected_difference": self.expected_difference,
            "created_at": self.created_at,
        }


class GoalReconciler:
    """Reconciles autonomous decisions with durable GoalRun authority (spec §9)."""

    def __init__(self, goal_run_store: Any = None):
        self._goal_run_store = goal_run_store
        self._active_revisions: dict[str, MissionRevision] = {}
        self._revisions: dict[str, list[MissionRevision]] = {}

    def retask_subtask(
        self,
        mission_id: str,
        goal_run_id: str,
        subtask_id: str,
        decision_id: str,
        reason: str,
        previous_executor: str,
        new_executor: str,
        previous_instructions: str,
        new_instructions: str,
        expected_difference: str = "",
        evidence_refs: list[str] | None = None,
    ) -> RetaskResult:
        """Retask single subtask execution approach without altering mission objective (spec §11)."""
        retask_rec = RetaskResult(
            retask_id=f"rtk_{uuid.uuid4().hex[:12]}",
            decision_id=decision_id,
            subtask_id=subtask_id,
            previous_attempt={
                "executor": previous_executor,
                "instructions": previous_instructions,
            },
            new_attempt={
                "executor": new_executor,
                "instructions": new_instructions,
            },
            retask_reason=reason,
            expected_difference=expected_difference
            or f"Switch from {previous_executor} to {new_executor}",
            created_at=time.time(),
        )
        return retask_rec

    def replan(
        self,
        mission_id: str,
        goal_run_id: str,
        decision_id: str,
        preserved_progress: list[str],
        invalidated_assumptions: list[str],
        cancelled_future_steps: list[str],
        new_goal_graph: list[dict[str, Any]],
        verification_requirements: list[str] | None = None,
    ) -> ReplanResult:
        """Replan remaining steps while strictly preserving accepted progress (spec §10)."""
        replan_rec = ReplanResult(
            replan_id=f"rpl_{uuid.uuid4().hex[:12]}",
            decision_id=decision_id,
            preserved_progress=list(preserved_progress),
            invalidated_assumptions=list(invalidated_assumptions),
            cancelled_future_steps=list(cancelled_future_steps),
            new_goal_graph=list(new_goal_graph),
            verification_requirements=list(verification_requirements or []),
            created_at=time.time(),
        )
        return replan_rec

    def revise_mission(
        self,
        mission_id: str,
        new_objective: str,
        reason: str,
        source: str = "USER",
        constraints: list[str] | None = None,
        parent_revision_id: str | None = None,
        previous_objective: str = "",
    ) -> MissionRevision:
        """Create explicit MissionRevision when objective or constraints change (spec §22)."""
        prev_rev = self._active_revisions.get(mission_id)
        parent_id = parent_revision_id or (prev_rev.revision_id if prev_rev else None)
        prev_obj = previous_objective or (prev_rev.objective if prev_rev else "")
        rev = MissionRevision(
            revision_id=f"rev_{uuid.uuid4().hex[:12]}",
            mission_id=mission_id,
            parent_revision_id=parent_id,
            previous_objective=prev_obj,
            objective=new_objective,
            constraints=list(constraints or []),
            reason=reason,
            source=source,
            created_at=time.time(),
        )
        self._active_revisions[mission_id] = rev
        self._revisions.setdefault(mission_id, []).append(rev)
        return rev

    def get_active_revision(self, mission_id: str) -> MissionRevision | None:
        return self._active_revisions.get(mission_id)

    def list_revisions(self, mission_id: str) -> list[MissionRevision]:
        return list(self._revisions.get(mission_id, []))
