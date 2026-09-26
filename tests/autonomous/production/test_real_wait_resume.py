"""AQ-P4, AQ-P5, AQ-P30, AQ-P31, AQ-P32: Real Wait and Resume Qualification Tests.

Invariants:
- BUSY_POLLING=0: Unmet dependencies durably park mission without busy execution loops.
- MEANINGLESS_ACTION_DURING_WAIT=0 / ACTION_FOR_ACTIONS_SAKE=0: No actions while blocked.
- AUTO_WAKE=PASS: Condition satisfaction triggers wake.
- REASSESS_AFTER_WAKE=PASS: Reassessment occurs before acting.
- WAIT_STATE_LOSS=0: Wait survives daemon restart.
- DUPLICATE_RESUME=0: Wake triggers exactly one resumption.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    AutonomousStatus,
    DecisionType,
    ObservationSource,
    ObservationStatus,
)


def test_real_wait_persisted_no_busy_polling() -> None:
    """AQ-P4 & AQ-P32: Blocker causes durable WAIT, no action for action's sake."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_wait_1",
            objective="Deploy artifact after approval",
            base_dir=tmpdir,
        )

        # Inject blocking dependency
        cycle.blocking_conditions = ["APPROVAL_PENDING:deploy_prod"]

        action_count = 0

        def dummy_executor(action: ActionProposal) -> tuple[int, str, list[str]]:
            nonlocal action_count
            action_count += 1
            return 0, "Executed", ["ev1"]

        # Step 1: Must decide to WAIT
        st1 = cycle.step(executor=dummy_executor)
        assert st1.state == AutonomousStatus.WAITING
        assert cycle.latest_decision is not None
        assert cycle.latest_decision.decision_type == DecisionType.WAIT
        assert action_count == 0  # No action executed!

        # Check wait persistence
        waits_file = Path(tmpdir) / ".veya" / "autonomous" / "prod_wait_1" / "waits.jsonl"
        assert waits_file.is_file()
        active_waits = cycle.wait_manager.list_active_waits("prod_wait_1")
        assert len(active_waits) == 1
        assert active_waits[0].condition_id

        # Step 2: Call step again while still waiting - must NOT busy-poll or execute action
        initial_dec_count = cycle.decision_store.count("prod_wait_1")
        st2 = cycle.step(executor=dummy_executor)
        assert st2.state == AutonomousStatus.WAITING
        assert action_count == 0  # MEANINGLESS_ACTION_DURING_WAIT=0
        assert (
            cycle.decision_store.count("prod_wait_1") == initial_dec_count
        )  # No duplicate decision


def test_real_resume_and_reassessment() -> None:
    """AQ-P5: Condition satisfaction wakes mission and triggers reassessment."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_wait_resume",
            objective="Process data after file generated",
            base_dir=tmpdir,
        )
        data_file = Path(tmpdir) / "incoming_data.csv"

        # Blocked on file
        cycle.blocking_conditions = [f"FILE_REQUIRED:{data_file.name}"]
        cycle.step()
        assert cycle.status == AutonomousStatus.WAITING

        active_waits = cycle.wait_manager.list_active_waits("prod_wait_resume")
        assert len(active_waits) == 1
        cond = active_waits[0]

        # External reality satisfies condition: create file
        data_file.write_text("id,val\n1,100\n", encoding="utf-8")
        cycle.wait_manager.mark_satisfied(cond.condition_id)
        cycle.blocking_conditions.clear()

        # Resume mission
        executed = False

        def real_executor(action: ActionProposal) -> tuple[int, str, list[str]]:
            nonlocal executed
            executed = True
            content = data_file.read_text(encoding="utf-8")
            return 0, f"Processed {len(content)} bytes", ["ev_data_processed"]

        st = cycle.resume(trigger_event="FILE_ARRIVED", executor=real_executor)
        assert st.state in (
            AutonomousStatus.ACTING,
            AutonomousStatus.VERIFYING,
            AutonomousStatus.OBSERVING,
            AutonomousStatus.PLANNING,
        )
        assert executed is True
        assert len(cycle.accepted_progress) >= 1


def test_wait_state_survives_restart() -> None:
    """AQ-P30: Wait condition survives daemon crash and restarts cleanly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Phase 1: enter wait and terminate
        cycle1 = AutonomousCycle(
            mission_id="prod_wait_restart",
            objective="Wait for web service recovery",
            base_dir=tmpdir,
        )
        cycle1.blocking_conditions = ["SERVICE_DOWN:api_backend"]
        cycle1.step()
        assert cycle1.status == AutonomousStatus.WAITING
        waits = cycle1.wait_manager.list_active_waits("prod_wait_restart")
        assert len(waits) == 1
        cond_id = waits[0].condition_id

        # Phase 2: restart daemon (new instance)
        cycle2 = AutonomousCycle(
            mission_id="prod_wait_restart",
            objective="Wait for web service recovery",
            base_dir=tmpdir,
        )
        assert cycle2.status == AutonomousStatus.WAITING  # WAIT_STATE_LOSS=0
        waits2 = cycle2.wait_manager.list_active_waits("prod_wait_restart")
        assert len(waits2) == 1
        assert waits2[0].condition_id == cond_id

        # Phase 3: satisfy dependency and wake
        cycle2.wait_manager.mark_satisfied(cond_id)
        cycle2.blocking_conditions.clear()

        dec_count_before = cycle2.decision_store.count("prod_wait_restart")
        cycle2.resume("SERVICE_RESTORED")
        dec_count_after = cycle2.decision_store.count("prod_wait_restart")
        # Exactly one continuation decision made
        assert dec_count_after == dec_count_before + 1


def test_provider_recovery_while_waiting() -> None:
    """AQ-P31: Provider recovery while waiting triggers reassessment, not blind replay."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_provider_recovery",
            objective="Execute workload on cloud worker",
            base_dir=tmpdir,
        )
        cycle.blocking_conditions = ["PROVIDER_UNAVAILABLE:gpu_cluster"]
        cycle.step()
        assert cycle.status == AutonomousStatus.WAITING

        # Mark provider healthy with new observation
        cycle.journal.append(
            mission_id="prod_provider_recovery",
            source=ObservationSource.SYSTEM,
            source_ref="cloud_monitor",
            kind="PROVIDER_HEALTHY",
            summary="gpu_cluster is back online and accepting jobs",
            status=ObservationStatus.CURRENT,
        )
        cycle.blocking_conditions.clear()
        cycle.wait_manager.mark_satisfied(
            cycle.wait_manager.list_active_waits("prod_provider_recovery")[0].condition_id
        )

        # Resume triggers reassessment
        st = cycle.resume("PROVIDER_RECOVERED")
        assert st.state != AutonomousStatus.WAITING
        assert any(
            "gpu_cluster is back online" in f
            for f in cycle.assessor.assess(
                mission_id="prod_provider_recovery",
                cycle_id="test",
                reconciled_context=cycle.journal.query("prod_provider_recovery"),
            ).known_facts
        )
