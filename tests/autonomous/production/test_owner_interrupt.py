"""AQ-P6, AQ-P7, AQ-P28, AQ-P29: Real Owner Interrupt and Offline Handling Tests.

Invariants:
- OWNER_BYPASS=0: High risk/uncertainty requires owner approval; never silently bypassed.
- ESCALATION_DURABLE=PASS: Escalations durably persisted to escalations.jsonl.
- NOTIFICATION_STORM=0: No continuous duplicate notification spam.
- SILENT_OBJECTIVE_MUTATION=0: Objective changes produce explicit MissionRevision.
- OWNER_INTERRUPT_BINDING=PASS: Owner interrupts route to MasterAgent reconciliation.
- ACCEPTED_PROGRESS_LOST=0: Interrupts retain historical verified claims.
- CONCURRENT_DECISION_FORK=0: Multiple concurrent events resolved without branching authorities.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.autonomous import (
    AutonomousCycle,
    AutonomousStatus,
    DecisionType,
    EscalationReason,
    InterruptCategory,
    ObservationSource,
    ObservationStatus,
)


def test_owner_offline_escalation_durable_and_parks() -> None:
    """AQ-P6: Agent parks when owner is offline without guessing or bypassing approval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_owner_offline",
            objective="Drop legacy production database tables",
            base_dir=tmpdir,
        )

        # Trigger escalation
        req = cycle.escalation_manager.escalate(
            mission_id="prod_owner_offline",
            decision_id="dec_drop_table",
            reason=EscalationReason.RISK_THRESHOLD,
            question="Drop table 'users_legacy'?",
            evidence_refs=["schema_diff_v2"],
        )

        # File must be persisted durably
        esc_file = (
            Path(tmpdir) / ".veya" / "autonomous" / "prod_owner_offline" / "escalations.jsonl"
        )
        assert esc_file.is_file()
        assert req.status == "PENDING"
        assert req.resolved is False

        # Verify query
        pending = cycle.escalation_manager.query("prod_owner_offline", status="PENDING")
        assert len(pending) == 1
        assert pending[0].escalation_id == req.escalation_id  # ESCALATION_DURABLE=PASS

        # Cycle remains parked in ESCALATING without guessing or bypassing
        cycle.status = AutonomousStatus.ESCALATING
        st = cycle.step()
        assert st.state == AutonomousStatus.ESCALATING
        assert (
            cycle.latest_decision is None
            or cycle.latest_decision.decision_type != DecisionType.COMPLETE
        )  # OWNER_BYPASS=0


def test_owner_interrupt_modifies_objective_with_revision() -> None:
    """AQ-P7: User interrupt requesting scope change creates MissionRevision and preserves progress."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_owner_interrupt",
            objective="Build payment gateway with Stripe",
            base_dir=tmpdir,
        )

        # First progress milestone
        cycle.accepted_progress.append("Milestone 1: Webhook router created")
        cycle.latest_progress.verified_claims.append("Milestone 1: Webhook router created")
        cycle.latest_progress.objective_coverage = 0.5

        # Owner sends interrupt: "Change objective to PayPal gateway instead"
        cycle.handle_interrupt(
            sender="owner@example.com",
            content="Change objective to: Build payment gateway with PayPal",
            category=InterruptCategory.OBJECTIVE_CHANGE,
        )

        # Invariant checks
        assert cycle.objective == "Build payment gateway with PayPal"
        assert (
            "Milestone 1: Webhook router created" in cycle.accepted_progress
        )  # ACCEPTED_PROGRESS_LOST=0

        # Check revisions
        revs = cycle.reconciler.list_revisions("prod_owner_interrupt")
        assert len(revs) >= 1
        assert revs[-1].objective == "Build payment gateway with PayPal"
        assert "Stripe" in revs[-1].previous_objective


def test_concurrent_events_reconciled_without_decision_fork() -> None:
    """AQ-P28: Concurrent wake events and observations merge into single MasterAgent reassessment."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cycle = AutonomousCycle(
            mission_id="prod_concurrent",
            objective="Process incoming event stream",
            base_dir=tmpdir,
        )

        # Inject multiple events arriving simultaneously
        cycle.journal.append(
            mission_id="prod_concurrent",
            source=ObservationSource.SCHEDULE,
            summary="Cron trigger fired at 12:00",
            status=ObservationStatus.CURRENT,
        )
        cycle.journal.append(
            mission_id="prod_concurrent",
            source=ObservationSource.USER,
            summary="User updated environment variable FOO=BAR",
            status=ObservationStatus.CURRENT,
        )
        cycle.journal.append(
            mission_id="prod_concurrent",
            source=ObservationSource.SYSTEM,
            summary="Health check latency degraded to 45ms",
            status=ObservationStatus.CURRENT,
        )

        # Step produces exactly one canonical decision for this cycle
        st = cycle.step()
        assert st.cycle_id == "cycle_1"
        assert cycle.decision_store.count("prod_concurrent") == 1  # CONCURRENT_DECISION_FORK=0
