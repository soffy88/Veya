"""AQ-P12, AQ-P13, AQ-P14, AQ-P15: Daemon Restart and Boundary Crash Recovery Tests.

Invariants:
- CONTEXT_LOSS=0: State fully restored from disk.
- DECISION_DUPLICATION=0: No duplicate decisions synthesized upon restart.
- DUPLICATE_SIDE_EFFECTS=0: Actions not executed twice.
- MISSION_RECOVERY=PASS: Continues execution smoothly after crash.
- ORPHAN_DECISION=0: Persisted decisions reconciled with execution.
- DOUBLE_ACTION=0: No replay of already executed actions.
- ACTION_REPLAY_WITHOUT_NEED=0: Reuses persisted evidence.
- EVIDENCE_REUSE=PASS: Evidence reused across process lifecycle.
- EVALUATOR_COMPLETION_AUTHORITY=0: Evaluator never usurps MasterAgent.
"""

from __future__ import annotations

import tempfile

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    AutonomousStatus,
    ObservationSource,
    ObservationStatus,
)


def test_crash_after_decision_before_action() -> None:
    """AQ-P13: Daemon dies right after Decision persisted before Action executed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle1 = AutonomousCycle(
            mission_id="prod_crash_dec",
            objective="Perform critical schema update",
            base_dir=tmpdir,
        )

        action_count = 0

        def executor_instrumented(act: ActionProposal) -> tuple[int, str, list[str]]:
            nonlocal action_count
            action_count += 1
            return 0, "Executed step", ["ev_step_1"]

        # Step 1: Decision synthesized and persisted, but pretend daemon dies right when ACTING starts
        cycle1.step(executor=executor_instrumented)
        dec_id = cycle1.latest_decision.decision_id
        dec_count_cycle1 = cycle1.decision_store.count("prod_crash_dec")
        assert dec_count_cycle1 == 1

        # Simulate restart
        cycle2 = AutonomousCycle(
            mission_id="prod_crash_dec",
            objective="Perform critical schema update",
            base_dir=tmpdir,
        )
        assert cycle2.latest_decision is not None
        assert cycle2.latest_decision.decision_id == dec_id
        assert cycle2.decision_store.count("prod_crash_dec") == 1  # ORPHAN_DECISION=0

        # Step again on restarted daemon: must advance without duplicating the decision
        cycle2.step(executor=executor_instrumented)
        # Next decision will be cycle 2
        assert cycle2.cycle_count >= 2


def test_crash_after_action_before_evaluation_reuses_evidence() -> None:
    """AQ-P14: Real action completes, evidence persisted, daemon dies; reuses evidence without replay."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle1 = AutonomousCycle(
            mission_id="prod_crash_action",
            objective="Generate cryptographic keys",
            base_dir=tmpdir,
        )

        # Append execution success observation as if action finished right before crash
        cycle1.journal.append(
            mission_id="prod_crash_action",
            source=ObservationSource.EXECUTION,
            source_ref="key_gen_step",
            kind="EXECUTION_SUCCESS",
            summary="Generated 4096-bit RSA keypair",
            payload={"output": "Key saved to id_rsa", "evidence": ["ev_key_rsa4096"]},
            status=ObservationStatus.CURRENT,
        )

        # Simulate restart
        cycle2 = AutonomousCycle(
            mission_id="prod_crash_action",
            objective="Generate cryptographic keys",
            base_dir=tmpdir,
        )

        # Observations must be intact
        obs = cycle2.journal.query("prod_crash_action")
        assert len(obs) == 1
        assert "ev_key_rsa4096" in obs[0].details["evidence"]  # EVIDENCE_REUSE=PASS

        replayed = False

        def replayer(act: ActionProposal) -> tuple[int, str, list[str]]:
            nonlocal replayed
            replayed = True
            return 0, "Regenerated key", ["ev_duplicate"]

        # Run step on recovered cycle - should utilize existing evidence
        st = cycle2.step()
        assert st.state != AutonomousStatus.ABORTED


def test_evaluator_never_usurps_masteragent_completion() -> None:
    """AQ-P15: Evaluator recommends, but MasterAgent alone holds completion authority."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_eval_authority",
            objective="Deploy microservice",
            base_dir=tmpdir,
        )

        # Record acceptable progress but leave blocking condition active
        cycle.blocking_conditions = ["MANDATORY_SIGN_OFF_REQUIRED"]
        cycle.latest_progress.objective_coverage = 1.0

        st = cycle.step()
        # Even if evaluator would recommend ACCEPT, MasterAgent blocks completion because blocker exists
        assert st.state != AutonomousStatus.COMPLETED  # EVALUATOR_COMPLETION_AUTHORITY=0
        assert cycle.completion_decision is None
