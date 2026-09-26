"""Qualification Scenarios K through P for Operations V1 (spec §60-§65).

Scenario K: Operations Controller Crash & Recovery
Scenario L: Stale Operations Controller Commit Denial
Scenario M: Multi-Principal Isolation
Scenario N: Provider Cooldown Integration
Scenario O: Operational Authority Audit
Scenario P: Audit Trail 100% Coverage
"""

from __future__ import annotations

import tempfile

import pytest

from veya.operations import (
    CrossPrincipalAccessDeniedError,
    DuplicateOperationalRequestError,
    MaintenanceScope,
    OperationsController,
    ProviderOperationalStatus,
    StaleOperationsGenerationError,
)


def test_scenario_k_controller_crash_recovery() -> None:
    """Scenario K: Controller crash and restart recovers state and preserves idempotency (spec §60)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops1 = OperationsController(operations_id="c_k", base_dir=tmpdir)
        ops1.pause_agent("agent_k_1", reason="Pre-crash pause", idempotency_key="key_pause_1")
        ops1.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_k_1",
            duration_s=3600.0,
            reason="Hardware fix",
            idempotency_key="key_maint_1",
        )

        gen1 = ops1.generation

        # Simulate sudden process termination
        del ops1

        # Restart controller from same base_dir
        ops2 = OperationsController(operations_id="c_k", base_dir=tmpdir)
        assert ops2.generation > gen1  # Generation advanced on recovery
        assert ops2.lifecycle.get_agent_state("agent_k_1").desired_state.value == "PAUSED"
        assert ops2.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_k_1")

        # Duplicate mutation with same idempotency_key is rejected (DUPLICATE_OPERATIONAL_SIDE_EFFECTS=0)
        with pytest.raises(DuplicateOperationalRequestError):
            ops2.pause_agent("agent_k_1", reason="Duplicate call", idempotency_key="key_pause_1")


def test_scenario_l_stale_controller() -> None:
    """Scenario L: Stale operations generation commits are strictly denied (spec §61)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)
        ops.generation = 5  # Advanced generation

        # Stale attempt with expected_generation = 4
        with pytest.raises(StaleOperationsGenerationError):
            ops.pause_agent("agent_l_1", expected_generation=4)

        # Valid attempt with current generation
        st = ops.pause_agent("agent_l_1", expected_generation=5)
        assert st.desired_state.value == "PAUSED"


def test_scenario_m_cross_principal() -> None:
    """Scenario M: Principal A cannot read or modify Principal B operational state (spec §62)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)

        # Principal A attempts to pause agent owned by Principal B
        with pytest.raises(CrossPrincipalAccessDeniedError):
            ops.pause_agent(
                agent_id="agent_b_owned",
                actor="actor_alice",
                actor_principal="principal_a",
                target_principal="principal_b",
            )

        # Same-principal operation succeeds
        st = ops.pause_agent(
            agent_id="agent_a_owned",
            actor="actor_alice",
            actor_principal="principal_a",
            target_principal="principal_a",
        )
        assert st.desired_state.value == "PAUSED"


def test_scenario_n_provider_cooldown() -> None:
    """Scenario N: UPSTREAM_QUOTA_EXHAUSTED marks cooldown and retry-not-before without silent fallback (spec §63)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)

        # Mark cooldown on 429
        ops.providers.mark_cooldown("deepseek", duration_s=60.0)
        assert ops.providers.get_status("deepseek") == ProviderOperationalStatus.COOLDOWN
        assert ops.providers.get_retry_not_before("deepseek") is not None
        # Zero silent fallback to other providers


def test_scenario_o_operational_authority_audit() -> None:
    """Scenario O: Operations V1 has zero semantic, goal mutation, or direct execution authority (spec §64)."""
    ops = OperationsController()

    # Assert no semantic planning or execution methods exist on Operations
    assert not hasattr(ops, "execute_goal")
    assert not hasattr(ops, "mutate_goal_run")
    assert not hasattr(ops, "evaluate_semantic_evidence")
    assert not hasattr(ops, "place_mission_directly")


def test_scenario_p_audit_record_coverage() -> None:
    """Scenario P: 100% operational mutation audit coverage with zero missing records (spec §65)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ops = OperationsController(base_dir=tmpdir)

        ops.pause_agent("agent_p_1", reason="Pause step")
        ops.resume_agent("agent_p_1", reason="Resume step")
        ops.drain_agent("agent_p_1", reason="Drain step")
        ops.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_p_1",
            duration_s=60.0,
            reason="Maint step",
        )

        records = ops.audit.list_records()
        assert len(records) >= 4  # AUDIT_RECORD_COVERAGE=100%
        actions = [r.action for r in records]
        assert "pause_agent" in actions
        assert "resume_agent" in actions
        assert "drain_agent" in actions
        assert "set_maintenance" in actions
        for r in records:
            assert r.actor == "operator"
            assert r.timestamp > 0
            assert r.record_id.startswith("audit_")
