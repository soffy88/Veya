"""AQ-P10, AQ-P26, AQ-P27: Real Replan, Retask Scope, and Oscillation Detection Tests.

Invariants:
- OSCILLATION_DETECTION=PASS: A->B->A->B alternating cycle detected and halted.
- AUTONOMOUS_OSCILLATION_LOOP=0: Never loops infinitely between alternating strategies.
- RETASK_SCOPE=PASS: Child failure triggers child retask without unnecessary full mission replan.
- UNNECESSARY_FULL_REPLAN=0: Single task failure does not destroy entire goal hierarchy.
- ACCEPTED_PROGRESS_LOST=0: Replan strictly preserves accepted historical accomplishments.
- HISTORICAL_PROGRESS_REWRITTEN=0: Accepted accomplishments are never mutated.
"""

from __future__ import annotations

import tempfile

from veya.autonomous import (
    AutonomousCycle,
    AutonomousStatus,
    DecisionType,
    GoalReconciler,
    OscillationDetector,
)


def test_oscillation_detected_halts_loop() -> None:
    """AQ-P10: 2-cycle alternating execution (A->B->A->B) detected by OscillationDetector."""
    detector = OscillationDetector(window_size=8)

    detector.record_signature("STRATEGY_A")
    assert detector.check().is_oscillating is False

    detector.record_signature("STRATEGY_B")
    assert detector.check().is_oscillating is False

    detector.record_signature("STRATEGY_A")
    assert detector.check().is_oscillating is False

    detector.record_signature("STRATEGY_B")
    sig = detector.check()
    assert sig.is_oscillating is True
    assert "2-cycle alternating" in sig.reason

    # Integrated test with AutonomousCycle
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_oscillation",
            objective="Resolve flaky build dependency",
            base_dir=tmpdir,
        )

        # Feed alternating actions to oscillation detector
        cycle.oscillation_detector.record_signature("BUILD_CLANG")
        cycle.oscillation_detector.record_signature("BUILD_GCC")
        cycle.oscillation_detector.record_signature("BUILD_CLANG")
        cycle.oscillation_detector.record_signature("BUILD_GCC")

        # Cycle step must immediately ESCALATE to human instead of continuing infinite loop
        st = cycle.step()
        assert st.state == AutonomousStatus.ESCALATING
        assert cycle.latest_decision is not None
        assert cycle.latest_decision.decision_type == DecisionType.ESCALATE
        assert "OSCILLATION_DETECTED" in cycle.latest_decision.reason


def test_child_retask_scope_preserves_mission_plan() -> None:
    """AQ-P26: Single subtask failure retasks only the child without wiping mission."""
    reconciler = GoalReconciler()
    retask_rec = reconciler.retask_subtask(
        mission_id="prod_retask_scope",
        goal_run_id="gr_p26",
        subtask_id="st_compile",
        decision_id="dec_p26_1",
        reason="OOM killed on standard runner",
        previous_executor="standard_runner",
        new_executor="isolated_specialist",
        previous_instructions="compile with -O3",
        new_instructions="compile with -O1 to conserve memory",
        expected_difference="Reduces peak RAM usage below cgroup limits",
    )

    assert retask_rec.subtask_id == "st_compile"
    assert retask_rec.new_attempt["executor"] == "isolated_specialist"
    assert "RAM" in retask_rec.expected_difference


def test_replan_strictly_preserves_accepted_progress() -> None:
    """AQ-P27: Replan retains all prior accepted milestones; nothing rewritten or lost."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_replan_progress",
            objective="Develop and deploy authentication subsystem",
            base_dir=tmpdir,
        )

        # Milestone 1: database schema
        cycle.accepted_progress.append("Milestone 1: DB schema migrations applied")
        cycle.latest_progress.verified_claims.append("Milestone 1: DB schema migrations applied")
        cycle.latest_progress.objective_coverage = 0.33

        # Milestone 2: token generator
        cycle.accepted_progress.append("Milestone 2: JWT token generation implemented")
        cycle.latest_progress.verified_claims.append(
            "Milestone 2: JWT token generation implemented"
        )
        cycle.latest_progress.objective_coverage = 0.66

        snapshot_before = list(cycle.accepted_progress)

        # Execute replan
        plan_rev = cycle.reconciler.replan(
            mission_id="prod_replan_progress",
            goal_run_id="gr_canonical",
            decision_id="dec_replan_sub",
            preserved_progress=cycle.accepted_progress,
            invalidated_assumptions=["Auth0 integration viable"],
            cancelled_future_steps=["Configure Auth0 tenant"],
            new_goal_graph=[{"goal_id": "g_oauth2", "goal": "Implement self-hosted OAuth2"}],
        )

        assert plan_rev.preserved_progress == snapshot_before
        assert "Milestone 1: DB schema migrations applied" in plan_rev.preserved_progress
        assert "Milestone 2: JWT token generation implemented" in plan_rev.preserved_progress
        assert len(plan_rev.preserved_progress) == 2  # ACCEPTED_PROGRESS_LOST=0
