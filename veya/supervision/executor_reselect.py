"""``mission.executor_reselect`` — failover as a first-class, receipted operation.

The spec's premise is that failover is a distinct operation from retask, and the
audit confirmed the code already agrees: retask is a task mutation whose
``next_task`` is mandatory (``retask.py:270``), while the existing failover path
re-runs the same goal with a different assignee (``runner.py:539``). What was
missing was that the decision was invisible — a three-key
``executor_substitution`` dict that cannot answer any of the questions §10 asks.

This module makes the existing decision canonical and durable. It deliberately
does **not** re-implement selection: ``select_mission_executor`` remains the
only authority for which executor is chosen, and this wraps it. It also never
calls retask.

What it adds:

* a request/response contract, so failover is callable and testable on its own;
* a receipt carrying the whole decision chain — every candidate, why each was
  kept or rejected, the health and capability snapshot, and who was admitted;
* §11 idempotency and §12 single-active-admission, carried by
  :mod:`server.goal_run.failover` rather than by an in-memory set;
* §16 structured events on the canonical event stream.

The separation the spec insists on, in one place: the *effect* of a task decides
nothing here. Task identity, task contract and GoalRun lineage are read and
carried through unchanged; only the executor identity differs between the failed
and the reselected attempt.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FAILOVER_EXHAUSTED",
    "ExecutorReselectionReceipt",
    "ExecutorReselectionRequest",
    "ReselectionOutcome",
    "reselect_executor",
]

#: §13 F1. Only reached when no eligible candidate exists at all.
FAILOVER_EXHAUSTED = "FAILOVER_EXHAUSTED"

#: §16 events. Emitted on the canonical event stream, not invented locally.
EVENT_UNAVAILABLE = "MISSION_EXECUTOR_UNAVAILABLE"
EVENT_REQUESTED = "MISSION_EXECUTOR_FAILOVER_REQUESTED"
EVENT_CANDIDATE_REJECTED = "MISSION_EXECUTOR_CANDIDATE_REJECTED"
EVENT_RESELECTED = "MISSION_EXECUTOR_RESELECTED"
EVENT_ADMITTED = "MISSION_EXECUTOR_ADMITTED"
EVENT_EXHAUSTED = "MISSION_EXECUTOR_FAILOVER_EXHAUSTED"


@dataclass(frozen=True)
class ExecutorReselectionRequest:
    """§3 input. ``preferred_executor`` is a preference, never a pin."""

    mission_id: str
    iteration_id: str
    task_id: str
    failure_event_id: str
    reason: str = ""
    preferred_executor: str | None = None
    exclude_executors: tuple[str, ...] = ()
    requested_by: str = "supervisor"
    project_root: str = ""
    goal_run_id: str = ""
    task_contract: dict[str, Any] | None = None
    required_capabilities: dict[str, Any] | None = None

    def idempotency_key(self) -> str:
        return ".".join(
            part or "-"
            for part in (
                self.mission_id,
                self.iteration_id,
                self.task_id,
                self.failure_event_id,
            )
        )


@dataclass
class ExecutorReselectionReceipt:
    """§10 output: enough to answer why each candidate was or was not chosen."""

    receipt_id: str
    mission_id: str
    iteration_id: str
    task_id: str
    goal_run_id: str
    previous_executor: str | None
    selected_executor: str | None
    reason: str
    candidate_executors: list[dict[str, Any]] = field(default_factory=list)
    rejected_candidates: list[dict[str, Any]] = field(default_factory=list)
    selection_policy: dict[str, Any] = field(default_factory=dict)
    health_snapshot: dict[str, str] = field(default_factory=dict)
    capability_snapshot: dict[str, Any] = field(default_factory=dict)
    admission_result: dict[str, Any] = field(default_factory=dict)
    execution_attempt_id: str | None = None
    requested_by: str = "supervisor"
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExecutorReselectionReceipt:
        """Rebuild a stored receipt.

        Needed by §11: a replayed request must hand back the *existing* receipt,
        not a freshly minted one with a new id, otherwise two callers holding
        "the same" receipt are indistinguishable and the attempt is not provably
        the same one.
        """
        return cls(
            receipt_id=str(payload.get("receipt_id") or ""),
            mission_id=str(payload.get("mission_id") or ""),
            iteration_id=str(payload.get("iteration_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            goal_run_id=str(payload.get("goal_run_id") or ""),
            previous_executor=payload.get("previous_executor"),
            selected_executor=payload.get("selected_executor"),
            reason=str(payload.get("reason") or ""),
            candidate_executors=list(payload.get("candidate_executors") or []),
            rejected_candidates=list(payload.get("rejected_candidates") or []),
            selection_policy=dict(payload.get("selection_policy") or {}),
            health_snapshot=dict(payload.get("health_snapshot") or {}),
            capability_snapshot=dict(payload.get("capability_snapshot") or {}),
            admission_result=dict(payload.get("admission_result") or {}),
            execution_attempt_id=payload.get("execution_attempt_id"),
            requested_by=str(payload.get("requested_by") or "supervisor"),
            timestamp=float(payload.get("timestamp") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "mission_id": self.mission_id,
            "iteration_id": self.iteration_id,
            "task_id": self.task_id,
            "goal_run_id": self.goal_run_id,
            "previous_executor": self.previous_executor,
            "selected_executor": self.selected_executor,
            "reason": self.reason,
            "candidate_executors": list(self.candidate_executors),
            "rejected_candidates": list(self.rejected_candidates),
            "selection_policy": dict(self.selection_policy),
            "health_snapshot": dict(self.health_snapshot),
            "capability_snapshot": dict(self.capability_snapshot),
            "admission_result": dict(self.admission_result),
            "execution_attempt_id": self.execution_attempt_id,
            "requested_by": self.requested_by,
            "timestamp": self.timestamp,
        }


@dataclass
class ReselectionOutcome:
    """The operation's return: a receipt plus whether execution may proceed."""

    ok: bool
    code: str
    receipt: ExecutorReselectionReceipt
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "replayed": self.replayed,
            "receipt": self.receipt.to_dict(),
        }


def _emit(event: str, **fields: Any) -> None:
    """Best-effort canonical event. Never let observability break failover."""
    try:
        from server.events import append_canonical_event

        append_canonical_event(event, fields, actor="supervisor")
    except Exception:
        pass


def _rejection_reason(candidate: Any, excluded: tuple[str, ...]) -> str | None:
    """Why this candidate is not selectable, or None when it still stands.

    The evidence is already computed on ``ExecutorCandidate`` by
    ``executor_candidates``; this only names it. That is the difference between a
    receipt and a guess.
    """
    if candidate.executor_id in excluded:
        return "EXCLUDED_BY_REQUEST"
    if not candidate.admission_supported:
        return "ADMISSION_UNSUPPORTED"
    if not candidate.capability_satisfied:
        return "CAPABILITY_MISMATCH"
    if candidate.local:
        return None
    if not candidate.reachable:
        return "UNREACHABLE"
    if not candidate.authenticated:
        return "UNAUTHENTICATED"
    if str(candidate.health).upper() == "UNAVAILABLE":
        return "HEALTH_UNAVAILABLE"
    return None


def reselect_executor(
    request: ExecutorReselectionRequest,
    *,
    previous_executor: str | None = None,
    failure_class: str | None = None,
) -> ReselectionOutcome:
    """Re-select an executor for an unfinished task and receipt the decision.

    Read-only with respect to task semantics: nothing here mutates the mission,
    the task, its contract or the GoalRun. It answers exactly one question —
    which admitted executor should run this task instead — and records why.

    Selection is delegated to ``select_mission_executor`` so this stays a
    wrapper and not a second implementation of registry selection (§18.9).
    """
    from server.goal_run import failover as failover_ledger
    from veya.supervision.runner import (
        _health_registry,
        executor_candidates,
        select_mission_executor,
    )

    _emit(
        EVENT_UNAVAILABLE,
        mission_id=request.mission_id,
        iteration_id=request.iteration_id,
        task_id=request.task_id,
        goal_run_id=request.goal_run_id,
        executor=previous_executor,
        reason=request.reason or failure_class or "",
        timestamp=time.time(),
    )
    _emit(
        EVENT_REQUESTED,
        mission_id=request.mission_id,
        iteration_id=request.iteration_id,
        task_id=request.task_id,
        goal_run_id=request.goal_run_id,
        executor=previous_executor,
        reason=request.reason or failure_class or "",
        preferred_executor=request.preferred_executor or "",
        timestamp=time.time(),
    )

    # Failover means *away from* the executor that failed. The caller may pass
    # more exclusions, but never selecting the failed executor is the operation's
    # own invariant, not an option: without it a persistently failing preferred
    # executor is re-selected every pass and the mission never advances.
    excluded = tuple(
        dict.fromkeys(name for name in (previous_executor, *request.exclude_executors) if name)
    )

    health = _health_registry()
    candidates = executor_candidates(
        required_capabilities=request.required_capabilities,
        health_registry=health,
        local_capable=True,
    )

    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        reason = _rejection_reason(candidate, excluded)
        if reason is not None:
            rejected.append({"executor_id": candidate.executor_id, "reason": reason})
            _emit(
                EVENT_CANDIDATE_REJECTED,
                mission_id=request.mission_id,
                iteration_id=request.iteration_id,
                task_id=request.task_id,
                goal_run_id=request.goal_run_id,
                executor=candidate.executor_id,
                reason=reason,
                timestamp=time.time(),
            )

    # §18.9: the registry decides, this function only routes to it.
    selected, eligible = select_mission_executor(
        requested=request.preferred_executor,
        required_capabilities=request.required_capabilities,
        health_registry=health,
        local_capable=True,
        allow_autonomous_external=True,
    )
    still_eligible = [
        candidate
        for candidate in eligible
        if candidate.executor_id not in excluded and candidate.eligible
    ]
    # §8: a preference that cannot be honoured must fall through to the next
    # eligible candidate, not end the search. Previously an excluded preference
    # left `selected` as None and the fallback below was skipped, so excluding a
    # failed executor exhausted the mission instead of reselecting.
    if selected is not None and (
        selected.executor_id in excluded or selected not in still_eligible
    ):
        selected = None
    if selected is None and still_eligible:
        selected = still_eligible[0]

    receipt = ExecutorReselectionReceipt(
        receipt_id=f"resel_{uuid.uuid4().hex[:16]}",
        mission_id=request.mission_id,
        iteration_id=request.iteration_id,
        task_id=request.task_id,
        goal_run_id=request.goal_run_id,
        previous_executor=previous_executor,
        selected_executor=selected.executor_id if selected else None,
        reason=request.reason or failure_class or "executor reselection requested",
        candidate_executors=[c.to_dict() for c in candidates],
        rejected_candidates=rejected,
        selection_policy={
            "preferred_executor": request.preferred_executor or "",
            "preferred_is_a_pin": False,
            "exclude_executors": list(excluded),
            "required_capabilities": dict(request.required_capabilities or {}),
            "eligible_after_exclusions": [c.executor_id for c in still_eligible],
        },
        health_snapshot={c.executor_id: str(c.health) for c in candidates},
        capability_snapshot={
            c.executor_id: {
                "capability_satisfied": c.capability_satisfied,
                "reachable": c.reachable,
                "authenticated": c.authenticated,
            }
            for c in candidates
        },
        requested_by=request.requested_by,
    )

    if selected is None:
        receipt.admission_result = {"accepted": False, "reason": FAILOVER_EXHAUSTED}
        _emit(
            EVENT_EXHAUSTED,
            mission_id=request.mission_id,
            iteration_id=request.iteration_id,
            task_id=request.task_id,
            goal_run_id=request.goal_run_id,
            executor=previous_executor or "",
            reason=FAILOVER_EXHAUSTED,
            candidates=len(candidates),
            timestamp=time.time(),
        )
        return ReselectionOutcome(ok=False, code=FAILOVER_EXHAUSTED, receipt=receipt)

    _emit(
        EVENT_RESELECTED,
        mission_id=request.mission_id,
        iteration_id=request.iteration_id,
        task_id=request.task_id,
        goal_run_id=request.goal_run_id,
        executor=selected.executor_id,
        previous_executor=previous_executor or "",
        reason=receipt.reason,
        timestamp=time.time(),
    )

    # §11/§12: the durable admission. Same mission, same task, same GoalRun; only
    # a new execution attempt follows from this.
    outcome = failover_ledger.admit(
        request.project_root,
        request.goal_run_id,
        mission_id=request.mission_id,
        iteration_id=request.iteration_id,
        task_id=request.task_id,
        failure_event_id=request.failure_event_id,
        receipt=receipt.to_dict(),
    )
    receipt.admission_result = outcome.to_dict()
    if not outcome.accepted:
        return ReselectionOutcome(
            ok=False,
            code=outcome.reason,
            receipt=receipt,
            replayed=outcome.replayed,
        )

    if outcome.replayed:
        # §11: the stored receipt is the answer. Returning this caller's freshly
        # built one would mint a second receipt_id for one attempt.
        stored = outcome.receipt or {}
        return ReselectionOutcome(
            ok=True,
            code="IDEMPOTENT_REPLAY",
            receipt=ExecutorReselectionReceipt.from_dict(stored),
            replayed=True,
        )

    receipt.execution_attempt_id = f"attempt_{uuid.uuid4().hex[:12]}"
    _emit(
        EVENT_ADMITTED,
        mission_id=request.mission_id,
        iteration_id=request.iteration_id,
        task_id=request.task_id,
        goal_run_id=request.goal_run_id,
        executor=selected.executor_id,
        attempt=receipt.execution_attempt_id,
        replayed=outcome.replayed,
        timestamp=time.time(),
    )
    return ReselectionOutcome(
        ok=True,
        code="IDEMPOTENT_REPLAY" if outcome.replayed else "RESELECTED",
        receipt=receipt,
        replayed=outcome.replayed,
    )
