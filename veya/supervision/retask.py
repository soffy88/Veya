"""Retask — turn a SupervisorReview into the next runtime action (spec §7/§24).

The runtime never guesses an action from prose: it reads the review's
``decision`` enum. Completion authority is enforced here — an executor cannot
reach DONE while the report still carries unresolved failures/blockers, and an
ESCALATE review maps to exactly one of the owner-only escalation codes or to
WAITING_EXTERNAL_SUPERVISOR.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from runtime.verification.models import VerificationGateResult, VerificationVerdict

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
from .runner import _active_l1_executors
from .store import MissionStore

_TERMINAL_DECISIONS = {ReviewDecision.accept, ReviewDecision.done}

# Execution report statuses that may be accepted. These are the success outcomes
# this plane actually emits (``GoalStatus.completed`` and the dispatch path's
# ``"executed"``). A blocked, failed, cancelled, partial or in-flight iteration is
# not acceptable work: an evidence chain only proves the mission *ran*, never
# that it *succeeded*.
_ACCEPTABLE_REPORT_STATUSES = frozenset({"completed", "executed"})


def _report_status_value(status: Any) -> str:
    """Normalize a report status to its bare value.

    The same status can arrive as a plain string (``"completed"``), as a
    ``GoalStatus`` member, or as a stringified member (``"GoalStatus.completed"``
    from ``str(enum)``). All three must judge acceptance identically.
    """
    value = getattr(status, "value", status)
    text = str(value).strip().lower()
    return text.rsplit(".", 1)[-1] if "." in text else text


_REDO_DECISIONS = {
    ReviewDecision.continue_,
    ReviewDecision.revise,
    ReviewDecision.retry,
    ReviewDecision.rollback,
}

# Concrete L1 worker identities, projected from ExecutorRegistry.snapshot().  A
# retask may preserve one of them, but it must never silently fall back to a
# retired executor when the identity is absent or invalid — a retired name is
# absent from the registry, so it is absent here without being listed.
# Resolved lazily (PEP 562) for the same import-cycle reason as
# ``orchestrated.L1_WORKERS``: the registry cannot be touched at import time.
_RETASK_WORKERS_CACHE: frozenset[str] | None = None


def _retask_workers() -> frozenset[str]:
    global _RETASK_WORKERS_CACHE
    if _RETASK_WORKERS_CACHE is None:
        _RETASK_WORKERS_CACHE = frozenset(_active_l1_executors())
    return _RETASK_WORKERS_CACHE


def __getattr__(name: str) -> Any:
    if name == "_RETASK_WORKERS":
        return _retask_workers()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class RetaskOutcome:
    mission_status: MissionStatus
    next_task: ExecutionTask | None = None
    escalation_code: EscalationCode | None = None
    reason: str = ""
    correction_scope: str = "TASK"
    plan_version: int | None = None
    retask_lineage: dict[str, Any] = field(default_factory=dict)


def _detect_escalation(review: SupervisorReview) -> EscalationCode | None:
    texts = " ".join([review.reason, *review.risk_notes]).lower()
    for trigger in ESCALATION_TRIGGERS:
        if trigger in texts:
            return classify_escalation(trigger)
    return None


def _normalize_next_task(raw: Any) -> str | None:
    """Normalize only the structured next_task shape already emitted by review."""

    if isinstance(raw, str):
        value = raw.strip()
        return value or None
    if isinstance(raw, dict):
        objective = raw.get("objective")
        if isinstance(objective, str) and objective.strip():
            return objective.strip()
    return None


def _normalize_worker(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    worker = raw.strip().lower()
    return worker if worker in _retask_workers() else None


def _relative_artifacts(raw: Any) -> list[str] | None:
    if raw is None:
        return []
    values = [raw] if isinstance(raw, str) else raw
    if not isinstance(values, (list, tuple)):
        return None
    result: list[str] = []
    for value in values:
        path = str(value).strip()
        parts = path.replace("\\", "/").split("/")
        if not path or path.startswith("/") or ".." in parts:
            return None
        if path not in result:
            result.append(path)
    return result


def _review_id(review: SupervisorReview) -> str:
    encoded = json.dumps(review.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _l1_entries(report: ExecutionReport | None) -> list[dict[str, Any]]:
    if report is None:
        return []
    return [
        item
        for item in report.runtime_evidence
        if isinstance(item, dict) and item.get("kind") == "l1_execution"
    ]


def _original_child_metadata(
    report: ExecutionReport | None,
    *,
    worker: str | None,
    parent_subtask_id: str | None,
) -> dict[str, Any] | None:
    """Resolve the original logical child without choosing a worker implicitly."""

    entries = _l1_entries(report)
    if not entries and report is not None:
        # Lightweight/canonical runners may expose the task graph through the
        # report change projection without emitting an L1 execution envelope.
        # Preserve that authoritative assignee identity for retask lineage;
        # never invent a worker when the report does not provide one.
        entries = [
            {
                "task_id": item.get("task_id"),
                "subtask_id": item.get("task_id"),
                "worker": item.get("assignee"),
                "required_artifacts": item.get("required_artifacts") or [],
                "dependency_context": item.get("dependency_context") or {},
            }
            for item in report.changes
            if isinstance(item, dict) and item.get("task_id")
        ]
    if parent_subtask_id:
        entries = [item for item in entries if str(item.get("subtask_id")) == parent_subtask_id]
    if worker:
        entries = [item for item in entries if _normalize_worker(item.get("worker")) == worker]
    if len(entries) != 1:
        return None
    return entries[0]


def _task_from_review(
    review: SupervisorReview, mission: Mission, report: ExecutionReport | None = None
) -> tuple[ExecutionTask | None, dict[str, Any]]:
    raw = review.next_task
    objective = _normalize_next_task(raw)
    structured: dict[str, Any] = raw if isinstance(raw, dict) else {}
    worker_key = next(
        (key for key in ("worker", "worker_type", "executor") if key in structured),
        None,
    )
    explicit_worker = _normalize_worker(structured.get(worker_key)) if worker_key else None
    parent_subtask_id = (
        str(structured.get("parent_subtask_id")) if structured.get("parent_subtask_id") else None
    )
    source = _original_child_metadata(
        report,
        worker=explicit_worker if worker_key is None or explicit_worker is not None else None,
        parent_subtask_id=parent_subtask_id,
    )
    worker = (
        explicit_worker
        if worker_key is not None
        else _normalize_worker(source.get("worker") if source else None)
    )
    if worker_key is not None and explicit_worker is None:
        source = None
    if source is not None and parent_subtask_id is None:
        parent_subtask_id = str(source.get("subtask_id")) if source.get("subtask_id") else None
    required_from_source = source.get("required_artifacts") if source else []
    if "required_artifacts" in structured:
        required_artifacts = _relative_artifacts(structured.get("required_artifacts"))
    else:
        required_artifacts = _relative_artifacts(required_from_source)
    dependency_context = structured.get("dependency_context")
    if not isinstance(dependency_context, dict):
        dependency_context = {}
    if source and isinstance(source.get("dependency_context"), dict):
        dependency_context = {**source["dependency_context"], **dependency_context}
    if structured.get("depends_on") is not None:
        dependency_context = {
            **dependency_context,
            "depends_on": list(structured.get("depends_on") or []),
        }
    dependency_ids = {str(value) for value in dependency_context.get("depends_on", []) if value}
    if report is not None and dependency_ids and "dependency_artifacts" not in dependency_context:
        dependency_artifacts = [
            dict(item)
            for item in report.artifacts
            if isinstance(item, dict)
            and str(item.get("subtask_id") or item.get("task_id") or "") in dependency_ids
            and item.get("materialized_path")
        ]
        if dependency_artifacts:
            dependency_context = {
                **dependency_context,
                "dependency_artifacts": dependency_artifacts,
            }
    artifact_acceptance = structured.get("artifact_acceptance_criteria")
    if not isinstance(artifact_acceptance, list):
        artifact_acceptance = [
            f"materialized required artifact exists: {path}" for path in required_artifacts or []
        ]
    lineage = {
        "raw_next_task": raw,
        "normalized_next_task": objective,
        "source_review_id": _review_id(review),
        "original_child_execution_id": (
            str(source.get("execution_id")) if source and source.get("execution_id") else None
        ),
        "retask_child_execution_id": None,
        "parent_subtask_id": parent_subtask_id,
        "plan_version": int(mission.authority.get("plan_version", 1)),
        "correction_index": int(mission.authority.get("correction_index", 0)) + 1,
        "worker_type": worker,
        "required_artifacts": required_artifacts if required_artifacts is not None else [],
        "artifact_acceptance_criteria": [str(item) for item in artifact_acceptance],
        "dependency_context": dependency_context,
    }
    if not objective:
        lineage["retask_block_reason"] = "RETASK_BLOCKED_INVALID_NEXT_TASK"
        return None, lineage
    if worker is None:
        lineage["retask_block_reason"] = "RETASK_BLOCKED_WORKER_UNRESOLVED"
        return None, lineage
    if report is not None and source is None:
        lineage["retask_block_reason"] = "RETASK_BLOCKED_ORIGINAL_SUBTASK_UNRESOLVED"
        return None, lineage
    if required_artifacts is None:
        lineage["retask_block_reason"] = "RETASK_BLOCKED_INVALID_REQUIRED_ARTIFACTS"
        return None, lineage
    task_id = str(structured.get("task_id") or f"{mission.mission_id}-it{review.iteration + 1}")
    acceptance = structured.get("acceptance")
    acceptance_items = [str(item) for item in acceptance] if isinstance(acceptance, list) else []
    side_effect = SideEffectClass.write
    return ExecutionTask(
        task_id=task_id,
        objective=objective,
        # Concrete L1 identity is carried in inputs/lineage because the
        # canonical ExecutorKind enum is intentionally broader than the L1
        # worker registry (Pi/Grok are valid workers too).
        executor=ExecutorKind.worker,
        inputs={
            "worker_type": worker,
            "required_artifacts": list(required_artifacts),
            "artifact_acceptance_criteria": [str(item) for item in artifact_acceptance],
            "dependency_context": dependency_context,
            "dependency_artifacts": list(dependency_context.get("dependency_artifacts") or []),
        },
        acceptance=acceptance_items
        + list(review.required_evidence)
        + list(review.acceptance_delta),
        side_effect_class=side_effect,
    ), lineage


def plan_retask(
    review: SupervisorReview,
    *,
    mission: Mission,
    iteration: int = 0,
    budget: MissionBudget | None = None,
    report: ExecutionReport | None = None,
    verification: VerificationGateResult | VerificationVerdict | None = None,
) -> RetaskOutcome:
    budget = budget or mission.budget

    if review.decision in _TERMINAL_DECISIONS:
        if review.decision is ReviewDecision.done and mission.status is not MissionStatus.accepted:
            return RetaskOutcome(
                MissionStatus.blocked,
                reason="cannot complete: DONE requires a persisted ACCEPTED state",
            )
        # Completion authority: acceptance/evidence/blockers must be satisfied.
        # A blocked / failed / partial iteration is not acceptable work no matter
        # how much evidence it managed to produce: the evidence chain only proves
        # the mission *ran*, never that it *succeeded*.
        if (
            report is not None
            and _report_status_value(report.status) not in _ACCEPTABLE_REPORT_STATUSES
        ):
            return RetaskOutcome(
                MissionStatus.blocked,
                reason=(
                    f"cannot complete: execution report status is "
                    f"{_report_status_value(report.status)!r}, not an accepted outcome"
                ),
            )
        if report is not None and (
            report.blocked_items
            or report.failures
            or any(
                isinstance(item, dict)
                and (
                    item.get("artifact_requirement") == "UNSATISFIED"
                    or bool(item.get("missing_required_artifacts"))
                )
                for item in [*report.runtime_evidence, *report.changes]
            )
        ):
            return RetaskOutcome(
                MissionStatus.blocked,
                reason="cannot complete: unresolved failures/blockers/artifact requirements remain",
            )
        if report is not None and not report.evidence_chain:
            return RetaskOutcome(
                MissionStatus.blocked,
                reason="cannot complete: execution produced no verifiable evidence",
            )
        if mission.verification_profile_id and (verification is None or not verification.passed):
            return RetaskOutcome(
                MissionStatus.blocked,
                reason="cannot complete: required verification gate did not pass",
            )
        if verification is not None and not verification.passed:
            outcome = getattr(verification, "outcome", "FAIL")
            return RetaskOutcome(
                MissionStatus.blocked,
                reason=f"cannot complete: verification did not pass ({outcome})",
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
        if review.correction_scope == "PLAN":
            if review.decision is not ReviewDecision.revise:
                return RetaskOutcome(
                    MissionStatus.blocked,
                    reason="plan-level correction requires the canonical REVISE decision",
                    correction_scope="PLAN",
                )
            return RetaskOutcome(
                MissionStatus.retasking,
                reason=review.reason or "plan-level correction requested",
                correction_scope="PLAN",
                plan_version=int(mission.authority.get("plan_version", 1)) + 1,
            )
        task, lineage = _task_from_review(review, mission, report)
        if task is None:
            return RetaskOutcome(
                MissionStatus.blocked,
                reason=str(
                    lineage.get("retask_block_reason") or "RETASK_BLOCKED_INVALID_NEXT_TASK"
                ),
                retask_lineage=lineage,
            )
        return RetaskOutcome(
            MissionStatus.retasking,
            next_task=task,
            reason=review.reason,
            correction_scope="TASK",
            retask_lineage=lineage,
        )

    return RetaskOutcome(MissionStatus.blocked, reason="unrecognized review decision")


def apply_review(
    store: MissionStore,
    mission: Mission,
    review: SupervisorReview,
    *,
    iteration: int = 0,
    report: ExecutionReport | None = None,
    verification: VerificationGateResult | VerificationVerdict | None = None,
) -> RetaskOutcome:
    """Persist the review, transition the mission, and emit the retask event."""

    store.append_raw_review(review)
    store.append_review(review)
    outcome = plan_retask(
        review,
        mission=mission,
        iteration=iteration,
        report=report,
        verification=verification,
    )
    if outcome.correction_scope == "PLAN":
        _request_plan_revision(store, mission, review, iteration, outcome)
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
        pending = outcome.next_task.to_dict()
        pending.update(outcome.retask_lineage)
        mission.authority["pending_task_retask"] = pending
        mission.status = outcome.mission_status
        store.save(mission)
        store.append_event(mission.mission_id, "RETASK_CREATED", pending)
    elif outcome.correction_scope == "PLAN":
        store.append_event(
            mission.mission_id,
            "PLAN_REVISION_REQUESTED",
            {
                "base_plan_version": int(mission.authority.get("plan_version", 1)),
                "reason": review.reason,
                "decision": str(review.decision),
                "correction_scope": "PLAN",
            },
        )
    if outcome.escalation_code is not None:
        store.append_event(
            mission.mission_id,
            "ESCALATED",
            {"code": str(outcome.escalation_code), "reason": outcome.reason},
        )
    return outcome


def _current_plan(mission: Mission) -> list[dict[str, object]]:
    plan = mission.authority.get("plan")
    if not isinstance(plan, list):
        plan = (mission.policies.execution_policy or {}).get("subtasks") or []
    return [dict(item) for item in plan if isinstance(item, dict)]


def _request_plan_revision(
    store: MissionStore,
    mission: Mission,
    review: SupervisorReview,
    iteration: int,
    outcome: RetaskOutcome,
) -> None:
    """Persist a plan correction request without running a second runtime."""

    current = _current_plan(mission)
    version = int(mission.authority.get("plan_version", 1))
    history = mission.authority.setdefault("plan_history", [])
    if current and not any(int(item.get("version", -1)) == version for item in history):
        history.append(
            {
                "version": version,
                "plan": current,
                "preserved_reason": review.reason,
                "iteration": iteration,
            }
        )
    mission.authority["pending_plan_revision"] = {
        "base_version": version,
        "reason": review.reason,
        "next_task": review.next_task,
        "decision": str(review.decision),
        "correction_scope": "PLAN",
    }
    mission.authority.setdefault("plan_version", version)
    mission.authority["last_correction_scope"] = "PLAN"
    # ``outcome.plan_version`` is the version that the canonical planner must
    # produce next; the current plan remains authoritative until then.
    if outcome.plan_version is not None:
        mission.authority["pending_plan_version"] = outcome.plan_version
    store.save(mission)


__all__ = ["RetaskOutcome", "apply_review", "plan_retask"]
