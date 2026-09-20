"""Retask — turn a SupervisorReview into the next runtime action (spec §7/§24).

The runtime never guesses an action from prose: it reads the review's
``decision`` enum. Completion authority is enforced here — an executor cannot
reach DONE while the report still carries unresolved failures/blockers, and an
ESCALATE review maps to exactly one of the owner-only escalation codes or to
WAITING_EXTERNAL_SUPERVISOR.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    EscalationCode,
    ExecutionReport,
    ExecutionTask,
    ExecutorKind,
    Mission,
    MissionBudget,
    MissionStatus,
    ReviewDecision,
    SideEffectClass,
    SupervisorReview,
)
from .policy import ESCALATION_TRIGGERS, classify_escalation
from .store import MissionStore

_TERMINAL_DECISIONS = {ReviewDecision.accept, ReviewDecision.done}
_REDO_DECISIONS = {
    ReviewDecision.continue_,
    ReviewDecision.revise,
    ReviewDecision.retry,
    ReviewDecision.rollback,
}


@dataclass
class RetaskOutcome:
    mission_status: MissionStatus
    next_task: ExecutionTask | None = None
    escalation_code: EscalationCode | None = None
    reason: str = ""


def _detect_escalation(review: SupervisorReview) -> EscalationCode | None:
    texts = " ".join([review.reason, *review.risk_notes]).lower()
    for trigger in ESCALATION_TRIGGERS:
        if trigger in texts:
            return classify_escalation(trigger)
    return None


def _task_from_review(review: SupervisorReview, mission: Mission) -> ExecutionTask | None:
    objective = (review.next_task or "").strip()
    if not objective:
        return None
    side_effect = SideEffectClass.write
    return ExecutionTask(
        task_id=f"{mission.mission_id}-it{review.iteration + 1}",
        objective=objective,
        executor=ExecutorKind.hicode,
        acceptance=list(review.required_evidence) + list(review.acceptance_delta),
        side_effect_class=side_effect,
    )


def plan_retask(
    review: SupervisorReview,
    *,
    mission: Mission,
    iteration: int = 0,
    budget: MissionBudget | None = None,
    report: ExecutionReport | None = None,
) -> RetaskOutcome:
    budget = budget or mission.budget

    if review.decision in _TERMINAL_DECISIONS:
        # Completion authority: acceptance/evidence/blockers must be satisfied.
        if report is not None and (report.blocked_items or report.failures):
            return RetaskOutcome(
                MissionStatus.blocked,
                reason="cannot complete: unresolved failures/blockers remain",
            )
        status = (
            MissionStatus.done if review.decision is ReviewDecision.done else MissionStatus.accepted
        )
        return RetaskOutcome(status, reason=review.reason)

    if review.decision is ReviewDecision.escalate:
        code = _detect_escalation(review)
        if code is not None:
            return RetaskOutcome(
                MissionStatus.waiting_owner, escalation_code=code, reason=review.reason
            )
        return RetaskOutcome(
            MissionStatus.waiting_external_supervisor,
            reason=review.reason or "escalated for external supervision",
        )

    if review.decision in _REDO_DECISIONS:
        if iteration + 1 >= budget.max_iterations:
            return RetaskOutcome(
                MissionStatus.blocked,
                reason=f"iteration budget exhausted ({budget.max_iterations})",
            )
        task = _task_from_review(review, mission)
        if task is None:
            return RetaskOutcome(
                MissionStatus.blocked,
                reason=f"{review.decision} review carried no next_task",
            )
        return RetaskOutcome(MissionStatus.retasking, next_task=task, reason=review.reason)

    return RetaskOutcome(MissionStatus.blocked, reason="unrecognized review decision")


def apply_review(
    store: MissionStore,
    mission: Mission,
    review: SupervisorReview,
    *,
    iteration: int = 0,
    report: ExecutionReport | None = None,
) -> RetaskOutcome:
    """Persist the review, transition the mission, and emit the retask event."""

    store.append_review(review)
    outcome = plan_retask(review, mission=mission, iteration=iteration, report=report)
    store.set_status(mission.mission_id, outcome.mission_status)
    store.append_event(
        mission.mission_id,
        "REVIEW_COMPLETED",
        {
            "supervisor": review.supervisor,
            "decision": str(review.decision),
            "next_status": str(outcome.mission_status),
        },
    )
    if outcome.next_task is not None:
        store.append_event(mission.mission_id, "RETASK_CREATED", outcome.next_task.to_dict())
    if outcome.escalation_code is not None:
        store.append_event(
            mission.mission_id,
            "ESCALATED",
            {"code": str(outcome.escalation_code), "reason": outcome.reason},
        )
    return outcome


__all__ = ["RetaskOutcome", "apply_review", "plan_retask"]
