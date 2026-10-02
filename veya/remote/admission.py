"""Admission as a decision distinct from the execution lifecycle.

The execution lifecycle answers "what happened to work that started". It
cannot answer "was this work ever allowed to start", because a refusal and a
runtime block both used to arrive as ``BLOCKED`` and therefore shared a state,
an aggregation bucket, and a set of release paths. Anything that treated
``BLOCKED`` as terminal was implicitly treating admission as part of the
lifecycle.

So admission gets its own vocabulary here, and the lifecycle is left alone
until every reader can distinguish the two.

    REQUESTED
       |
       +-- ACCEPTED  -> execution lifecycle (QUEUED ... terminal)
       |
       +-- REJECTED  -> terminal admission result; no execution exists
       |
       +-- DEFERRED  -> a decision is pending (approval, capacity, health)

This module is read-only with respect to existing behaviour: it classifies and
labels, and it does not move any record.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "AdmissionDecision",
    "AdmissionReason",
    "AdmissionStatus",
    "admission_for_blocker",
    "is_admission_refusal",
]


class AdmissionStatus(StrEnum):
    """Outcome of the admission gate, before any execution exists."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


class AdmissionReason(StrEnum):
    """Why admission reached the decision it did.

    Grouped by the layer that owns the answer, so an operator is pointed at the
    right one: a capability gap is an executor problem, a policy refusal is a
    permission problem, and neither is a provider problem.
    """

    #: nothing refused; the work was admitted
    NONE = "NONE"

    # ── executor / capability ──
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    WRITE_NOT_QUALIFIED = "WRITE_NOT_QUALIFIED"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"
    EXECUTOR_DISABLED = "EXECUTOR_DISABLED"
    EXECUTOR_HEALTH_FAILURE = "EXECUTOR_HEALTH_FAILURE"
    EXECUTOR_NOT_ADMITTED = "EXECUTOR_NOT_ADMITTED"

    # ── policy / workspace ──
    POLICY_DENIED = "POLICY_DENIED"
    WORKSPACE_DENIED = "WORKSPACE_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

    # ── genuinely runtime, not admission ──
    RUNTIME_BLOCK = "RUNTIME_BLOCK"


@dataclass(frozen=True)
class AdmissionDecision:
    """A labelled admission outcome.

    ``admitted`` is the only value that authorises an execution to start. A
    record can be terminal and still not have been admitted, which is precisely
    the distinction the lifecycle cannot express on its own.
    """

    status: AdmissionStatus
    reason: AdmissionReason = AdmissionReason.NONE
    failure_class: str | None = None
    detail: str = ""

    @property
    def admitted(self) -> bool:
        return self.status is AdmissionStatus.ACCEPTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "admission_status": str(self.status),
            "admission_reason": str(self.reason),
            "admission_failure_class": self.failure_class,
            "admission_detail": self.detail,
        }


ACCEPTED = AdmissionDecision(status=AdmissionStatus.ACCEPTED)


def is_admission_refusal(decision: AdmissionDecision) -> bool:
    """True when the work was refused before execution."""

    return decision.status is not AdmissionStatus.ACCEPTED


#: The blocker strings the adapter already produces, mapped to the reason they
#: actually mean. Centralised so the classification is one table rather than a
#: judgement repeated at each call site — which is how BLOCKED came to carry
#: two meanings in the first place.
_BLOCKER_REASONS: dict[str, tuple[AdmissionReason, str]] = {
    "REQUIRED_CAPABILITY_UNAVAILABLE": (
        AdmissionReason.CAPABILITY_MISSING,
        "EXECUTOR_CAPABILITY_MISMATCH",
    ),
    "PINNED_EXECUTOR_NOT_WRITE_QUALIFIED": (
        AdmissionReason.WRITE_NOT_QUALIFIED,
        "EXECUTOR_CAPABILITY_MISMATCH",
    ),
    "WORKER_NOT_WRITE_QUALIFIED": (
        AdmissionReason.WRITE_NOT_QUALIFIED,
        "EXECUTOR_CAPABILITY_MISMATCH",
    ),
    "WORKER_UNAVAILABLE": (AdmissionReason.EXECUTOR_UNAVAILABLE, "EXECUTOR_UNAVAILABLE"),
    "EXECUTOR_DISABLED": (AdmissionReason.EXECUTOR_DISABLED, "EXECUTOR_DISABLED"),
    "EXECUTOR_HEALTH_FAILURE": (AdmissionReason.EXECUTOR_HEALTH_FAILURE, "EXECUTOR_HEALTH_FAILURE"),
    "EXECUTOR_NOT_ADMITTED": (AdmissionReason.EXECUTOR_NOT_ADMITTED, "EXECUTOR_UNAVAILABLE"),
    "ERROR_REPETITION_GUARD": (AdmissionReason.POLICY_DENIED, "EXECUTOR_UNAVAILABLE"),
}


def admission_for_blocker(blocker: str, detail: str = "") -> AdmissionDecision:
    """Classify an existing blocker without changing what it does.

    An unrecognised blocker is reported as a runtime block rather than an
    admission refusal. Defaulting the other way would let an unknown refusal
    claim it never reached admission, which is the error this module exists to
    prevent.
    """

    key = str(blocker or "")
    mapped = _BLOCKER_REASONS.get(key)
    if mapped is None:
        return AdmissionDecision(
            status=AdmissionStatus.REJECTED,
            reason=AdmissionReason.RUNTIME_BLOCK,
            detail=detail or key,
        )
    reason, failure_class = mapped
    return AdmissionDecision(
        status=AdmissionStatus.REJECTED,
        reason=reason,
        failure_class=failure_class,
        detail=detail or key,
    )
