"""AQ-P16, AQ-P17, AQ-P18: Real Budget Control and Autonomous Risk Gate Qualification Tests.

Invariants:
- BUDGET_VISIBLE_TO_DECISION=PASS: Low or exhausted budget visible to MasterAgent.
- BUDGET_OVERRUN_WITHOUT_DECISION=0: Budget exhaustion triggers semantic decision, not silent kill.
- HIGH_RISK_EXECUTED_WITHOUT_APPROVAL=0: High risk actions routed to owner escalation.
- POLICY_BYPASS=0: Security / safety policy cannot be bypassed autonomously.
"""

from __future__ import annotations

import tempfile

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    AutonomousRiskGate,
    AutonomousStatus,
    DecisionType,
    RiskLevel,
)


def test_budget_exhaustion_triggers_explicit_decision() -> None:
    """AQ-P16: Finite budget exhaustion fails preconditions and escalates to MasterAgent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Budget of only 2 actions
        cycle = AutonomousCycle(
            mission_id="prod_budget_limit",
            objective="Sync 1000 items in batch",
            base_dir=tmpdir,
            max_actions=2,
            max_cost=2.0,
        )

        def dummy_step(act: ActionProposal) -> tuple[int, str, list[str]]:
            return 0, "Synced chunk", ["ev_chunk"]

        # Action 1: OK
        cycle.step(executor=dummy_step)
        assert cycle.budget_controller.action_count == 1
        assert cycle.budget_controller.can_execute() is True

        # Action 2: OK
        cycle.step(executor=dummy_step)
        assert cycle.budget_controller.action_count == 2
        assert cycle.budget_controller.can_execute() is False

        # Action 3: Budget exhausted! Preconditions must reject ACT and synthesize ESCALATE
        st3 = cycle.step(executor=dummy_step)
        assert st3.state == AutonomousStatus.ESCALATING
        assert cycle.latest_decision is not None
        assert cycle.latest_decision.decision_type == DecisionType.ESCALATE
        assert "BUDGET_EXHAUSTED" in cycle.latest_decision.reason  # BUDGET_VISIBLE_TO_DECISION=PASS


def test_autonomous_risk_gate_intercepts_dangerous_action() -> None:
    """AQ-P18: Destructive commands flagged as REQUIRES_OWNER and never executed without approval."""
    gate = AutonomousRiskGate()

    # Low risk
    lvl1, _r1 = gate.assess_action("GIT_STATUS", {})
    assert lvl1 == RiskLevel.LOW

    # High risk / destructive command
    lvl2, _r2 = gate.assess_action("BASH_COMMAND", {"command": "rm -rf /var/data"})
    assert lvl2 == RiskLevel.REQUIRES_OWNER
    assert gate.requires_escalation(lvl2) is True  # HIGH_RISK_EXECUTED_WITHOUT_APPROVAL=0

    # Out of boundary access
    lvl3, _r3 = gate.assess_action("WRITE_FILE", {"path": "/etc/shadow"})
    assert lvl3 == RiskLevel.REQUIRES_OWNER
    assert gate.requires_escalation(lvl3) is True  # POLICY_BYPASS=0
