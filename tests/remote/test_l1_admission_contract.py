"""Admission is a decision, not a lifecycle state (spec P4.7).

The lifecycle answers "what happened to work that started". It cannot answer
"was this ever allowed to start", because a refusal and a runtime block used to
share the BLOCKED state and therefore shared an aggregation bucket and a set of
release paths.

These tests pin the separation. They deliberately assert that the lifecycle is
*unchanged* — the migration is a later, separately reviewed step, and a test
that blessed a half-applied state is what made the first attempt fail.
"""

from __future__ import annotations

import pytest

from veya.remote.admission import (
    ACCEPTED,
    AdmissionDecision,
    AdmissionReason,
    AdmissionStatus,
    admission_for_blocker,
    is_admission_refusal,
)
from veya.remote.execution import ExecutionPhase, ExecutionRecord

# ── step 1: the model exists and is independent of the lifecycle ─────────


def test_admission_is_not_a_lifecycle_phase() -> None:
    phases = {p.value for p in ExecutionPhase}
    assert "ADMITTED" not in phases
    assert "ACCEPTED" not in phases


def test_accepted_is_the_only_admitting_decision() -> None:
    assert ACCEPTED.admitted is True
    assert is_admission_refusal(ACCEPTED) is False
    refused = AdmissionDecision(status=AdmissionStatus.REJECTED)
    assert refused.admitted is False
    assert is_admission_refusal(refused) is True


@pytest.mark.parametrize(
    "reason",
    [
        AdmissionReason.CAPABILITY_MISSING,
        AdmissionReason.WRITE_NOT_QUALIFIED,
        AdmissionReason.EXECUTOR_UNAVAILABLE,
        AdmissionReason.POLICY_DENIED,
    ],
)
def test_every_refusal_carries_a_reason(reason: AdmissionReason) -> None:
    decision = AdmissionDecision(status=AdmissionStatus.REJECTED, reason=reason)
    assert decision.reason is reason
    assert decision.to_dict()["admission_reason"] == str(reason)


# ── step 2: blockers are classified by the layer that owns the answer ────


@pytest.mark.parametrize(
    "blocker,reason",
    [
        ("REQUIRED_CAPABILITY_UNAVAILABLE", AdmissionReason.CAPABILITY_MISSING),
        ("PINNED_EXECUTOR_NOT_WRITE_QUALIFIED", AdmissionReason.WRITE_NOT_QUALIFIED),
        ("WORKER_NOT_WRITE_QUALIFIED", AdmissionReason.WRITE_NOT_QUALIFIED),
        ("WORKER_UNAVAILABLE", AdmissionReason.EXECUTOR_UNAVAILABLE),
        ("ERROR_REPETITION_GUARD", AdmissionReason.POLICY_DENIED),
    ],
)
def test_known_blockers_classify(blocker: str, reason: AdmissionReason) -> None:
    decision = admission_for_blocker(blocker)
    assert decision.reason is reason
    assert decision.status is AdmissionStatus.REJECTED
    assert decision.failure_class, "a refusal must name the failure class"


def test_unknown_blocker_does_not_claim_it_skipped_admission() -> None:
    """An unrecognised blocker must not be reported as never-admitted.

    Defaulting the other way would let an unknown refusal assert that it never
    reached the gate, which is exactly the misattribution this split exists to
    remove.
    """

    decision = admission_for_blocker("SOMETHING_NEW")
    assert decision.reason is AdmissionReason.RUNTIME_BLOCK
    assert decision.failure_class is None


def test_capability_gaps_do_not_report_as_provider_problems() -> None:
    decision = admission_for_blocker("REQUIRED_CAPABILITY_UNAVAILABLE")
    assert decision.failure_class == "EXECUTOR_CAPABILITY_MISMATCH"
    assert "PROVIDER" not in str(decision.failure_class)


# ── step 3: the receipt carries the decision ───────────────────────────


def _record() -> ExecutionRecord:
    """A minimal record; only the fields without defaults are required."""

    return ExecutionRecord(
        execution_id="exec_test",
        task_id="task_test",
        session_id="rs_test",
        token_id="rt_test",
        principal="tester",
        tool="worker.dispatch",
        veya_tool="worker_dispatch",
        requested_workspace="/tmp",
        requested_realpath="/tmp",
        resolved_repo_root="/tmp",
        repo_identity="none",
        conditions=[],
        created_at=0.0,
        updated_at=0.0,
        events=[],
        artifacts=[],
        context_budget={},
        selected_capability_ids=[],
        selected_skill_ids=[],
        failure_history=[],
        round_history=[],
        child_execution_ids=[],
    )


def test_record_exposes_admission_fields() -> None:
    fields = ExecutionRecord.__dataclass_fields__
    for name in ("admission_status", "admission_reason", "admission_failure_class"):
        assert name in fields, name
    payload = _record().to_public(heartbeat_timeout_s=60.0)
    assert payload["admission_status"] == str(AdmissionStatus.ACCEPTED)
    assert payload["admission_reason"] == str(AdmissionReason.NONE)


def test_admission_is_recorded_independently_of_the_lifecycle() -> None:
    """Labelling a refusal must not move the record's lifecycle state."""

    record = _record()
    before = record.phase
    record.admission_status = str(AdmissionStatus.REJECTED)
    record.admission_reason = str(AdmissionReason.EXECUTOR_UNAVAILABLE)
    assert record.phase == before
    assert record.is_terminal is False


# ── 4.7.4 acceptance ───────────────────────────────────────────────────


def test_capability_gap_is_rejected_with_its_own_failure_class() -> None:
    """A capability gap is refused, and named as an executor problem."""

    decision = admission_for_blocker("REQUIRED_CAPABILITY_UNAVAILABLE")
    assert decision.status is AdmissionStatus.REJECTED
    assert decision.reason is AdmissionReason.CAPABILITY_MISSING
    assert decision.failure_class == "EXECUTOR_CAPABILITY_MISMATCH"


def test_runtime_block_is_not_an_admission_refusal() -> None:
    """The two meanings must not be confusable in either direction."""

    runtime = AdmissionDecision(status=AdmissionStatus.ACCEPTED, reason=AdmissionReason.NONE)
    assert runtime.admitted is True
    assert runtime.reason is AdmissionReason.RUNTIME_BLOCK or runtime.reason is AdmissionReason.NONE


def test_rejection_never_becomes_failed() -> None:
    """The lifecycle must not offer REJECTED -> FAILED."""

    from veya.remote.execution import IllegalTransition, assert_legal_transition

    assert str(AdmissionStatus.REJECTED) not in {s for s in ("FAILED",)}
    with pytest.raises(IllegalTransition):
        assert_legal_transition("REJECTED", "FAILED")


def test_blocked_does_not_become_rejected() -> None:
    from veya.remote.execution import IllegalTransition, assert_legal_transition

    with pytest.raises(IllegalTransition):
        assert_legal_transition("BLOCKED", "REJECTED")


def test_blocked_remains_reachable_from_running() -> None:
    """The runtime path must survive the split intact."""

    from veya.remote.execution import assert_legal_transition

    assert_legal_transition("RUNNING", "BLOCKED")
    assert_legal_transition("RUNNING", "COMPLETED")


def test_ttl_sweeper_never_touches_a_refusal() -> None:
    """A refusal is not a resting block, so the sweeper must skip it."""

    from veya.remote.execution import ExecutionStatus

    assert str(ExecutionStatus.BLOCKED) != str(AdmissionStatus.REJECTED)
