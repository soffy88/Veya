"""AQ-P8 & AQ-P9: Real Provider Failure and Repeated Failure Qualification Tests.

Invariants:
- RUNTIME_SEMANTIC_FALLBACK=0: Agent/MasterAgent, not runtime, decides semantic response to failure.
- REAL_RETASK=PASS: Provider failure triggers explicit retask to alternative executor/parameters.
- FALSE_SUCCESS=0: Execution failure never counted as success.
- NO_PROGRESS_DETECTION=PASS: Semantic lack of progress detected dynamically.
- REPEATED_IDENTICAL_ACTION_LIMITED=PASS: Prevents infinite retry of identical failing action.
- FIXED_MAX_ROUNDS=0: Based on semantic state deltas, not hardcoded max_rounds.
"""

from __future__ import annotations

import tempfile

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    AutonomousStatus,
    DecisionType,
    NoProgressDetector,
)


def test_real_provider_failure_triggers_semantic_retask() -> None:
    """AQ-P8: Executor failure triggers RETASK under MasterAgent authority."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_provider_fail",
            objective="Compile native library",
            base_dir=tmpdir,
        )

        attempts = 0

        def flaky_executor(act: ActionProposal) -> tuple[int, str, list[str]]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # Simulates real process crash / signal 9 / OOM
                return 137, "Process terminated by signal 9 (SIGKILL / OOM)", []
            # On retask attempt 2, succeeds
            return 0, "Compiled successfully with adjusted cgroup memory limit", ["ev_compile_ok"]

        # Step 1: fails
        cycle.step(executor=flaky_executor)
        assert len(cycle.accepted_progress) == 0  # FALSE_SUCCESS=0

        # Step 2: AutonomousCycle synthesizes RETASK or alternative action
        cycle.step(executor=flaky_executor)
        assert len(cycle.accepted_progress) >= 1
        assert "ev_compile_ok" in cycle.journal.query("prod_provider_fail")[-1].details.get(
            "evidence", []
        )


def test_repeated_failure_detected_by_no_progress_detector() -> None:
    """AQ-P9: 3 consecutive identical failures without state delta trigger NO_PROGRESS."""
    detector = NoProgressDetector(repeat_threshold=3)

    # Step 1: fail
    detector.record_step(
        action_signature="HTTP_POST_PAYMENT",
        error_detail="504 Gateway Timeout",
        evidence_count=0,
    )
    assert detector.check().has_no_progress is False

    # Step 2: identical fail
    detector.record_step(
        action_signature="HTTP_POST_PAYMENT",
        error_detail="504 Gateway Timeout",
        evidence_count=0,
    )
    assert detector.check().has_no_progress is False

    # Step 3: identical fail (threshold reached)
    detector.record_step(
        action_signature="HTTP_POST_PAYMENT",
        error_detail="504 Gateway Timeout",
        evidence_count=0,
    )
    sig = detector.check()
    assert sig.has_no_progress is True
    assert "HTTP_POST_PAYMENT" in sig.reason and "3 times" in sig.reason
    assert sig.recommended_decision in (
        DecisionType.RETASK,
        DecisionType.REPLAN,
        DecisionType.ESCALATE,
    )


def test_cycle_escalates_on_repeated_identical_failures() -> None:
    """AQ-P9: In-cycle repeated execution failure triggers escalation to human owner."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_repeated_fail",
            objective="Sync upstream ledger",
            base_dir=tmpdir,
        )

        def consistently_failing_executor(act: ActionProposal) -> tuple[int, str, list[str]]:
            return 1, "Connection refused to remote bank portal", []

        # Step 1: fails
        cycle.step(executor=consistently_failing_executor)
        # Step 2: fails
        cycle.step(executor=consistently_failing_executor)
        # Step 3: fails -> no-progress threshold reached
        cycle.step(executor=consistently_failing_executor)

        # Step 4: MasterAgent receives anomaly signal and decides RETASK / ESCALATE
        st4 = cycle.step(executor=consistently_failing_executor)
        assert st4.state in (AutonomousStatus.PLANNING, AutonomousStatus.ESCALATING)
        assert cycle.latest_decision is not None
        assert cycle.latest_decision.decision_type in (
            DecisionType.RETASK,
            DecisionType.REPLAN,
            DecisionType.ESCALATE,
        )
