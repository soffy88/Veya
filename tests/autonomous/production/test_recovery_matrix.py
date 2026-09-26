"""AQ-P40: Comprehensive State Recovery Matrix Tests.

Covers recovery across all cognitive states:
- OBSERVING
- ASSESSING
- ACTING
- VERIFYING
- WAITING
- ESCALATING
"""

from __future__ import annotations

import tempfile

from veya.autonomous import (
    AutonomousCycle,
    AutonomousStatus,
    EscalationReason,
)


def test_recovery_matrix_observing_state() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        c1 = AutonomousCycle("m_obs", "Test observing recovery", base_dir=tmpdir)
        c1.status = AutonomousStatus.OBSERVING
        c1._persist_state()

        c2 = AutonomousCycle("m_obs", "Test observing recovery", base_dir=tmpdir)
        assert c2.status == AutonomousStatus.OBSERVING


def test_recovery_matrix_assessing_state() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        c1 = AutonomousCycle("m_assess", "Test assessing recovery", base_dir=tmpdir)
        c1.status = AutonomousStatus.ASSESSING
        c1.open_questions = ["Is server alive?"]
        c1._persist_state()

        c2 = AutonomousCycle("m_assess", "Test assessing recovery", base_dir=tmpdir)
        assert c2.status == AutonomousStatus.ASSESSING
        assert c2.open_questions == ["Is server alive?"]


def test_recovery_matrix_acting_state() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        c1 = AutonomousCycle("m_acting", "Test acting recovery", base_dir=tmpdir)
        c1.status = AutonomousStatus.ACTING
        c1.step()
        c1._persist_state()

        c2 = AutonomousCycle("m_acting", "Test acting recovery", base_dir=tmpdir)
        assert c2.status in (
            AutonomousStatus.ACTING,
            AutonomousStatus.PLANNING,
            AutonomousStatus.VERIFYING,
        )
        assert c2.latest_decision is not None


def test_recovery_matrix_verifying_state() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        c1 = AutonomousCycle("m_verifying", "Test verifying recovery", base_dir=tmpdir)
        c1.status = AutonomousStatus.VERIFYING
        c1.last_evaluation_id = "eval_12345"
        c1._persist_state()

        c2 = AutonomousCycle("m_verifying", "Test verifying recovery", base_dir=tmpdir)
        assert c2.status == AutonomousStatus.VERIFYING


def test_recovery_matrix_waiting_state() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        c1 = AutonomousCycle("m_waiting", "Test waiting recovery", base_dir=tmpdir)
        c1.blocking_conditions = ["EXTERNAL_JOB_RUNNING"]
        c1.step()
        assert c1.status == AutonomousStatus.WAITING

        c2 = AutonomousCycle("m_waiting", "Test waiting recovery", base_dir=tmpdir)
        assert c2.status == AutonomousStatus.WAITING
        assert "EXTERNAL_JOB_RUNNING" in c2.blocking_conditions


def test_recovery_matrix_escalating_state() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        c1 = AutonomousCycle("m_escalating", "Test escalating recovery", base_dir=tmpdir)
        c1.escalation_manager.escalate(
            mission_id="m_escalating",
            decision_id="dec_esc",
            reason=EscalationReason.PERMISSION_REQUIRED,
            question="Need root token",
        )
        c1.status = AutonomousStatus.ESCALATING
        c1._persist_state()

        c2 = AutonomousCycle("m_escalating", "Test escalating recovery", base_dir=tmpdir)
        assert c2.status == AutonomousStatus.ESCALATING
        pending = c2.escalation_manager.query("m_escalating", status="PENDING")
        assert len(pending) == 1
        assert pending[0].reason == EscalationReason.PERMISSION_REQUIRED
