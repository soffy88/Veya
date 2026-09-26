"""Tests for Rollout and Rolling Upgrade Coordinator (Phase O6)."""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import AgentInstance, AgentInstanceStatus, FleetController
from veya.operations.models import HealthState, RolloutStatus
from veya.operations.rollout import RolloutCoordinator, StaleGenerationCommitError


def test_successful_rolling_upgrade() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_rollout", base_dir=tmpdir)
        for i in range(1, 5):
            controller.register_agent(
                AgentInstance(
                    agent_instance_id=f"agent_ro_{i}",
                    fleet_id="f_rollout",
                    status=AgentInstanceStatus.READY,
                )
            )

        coordinator = RolloutCoordinator(controller)
        plan = coordinator.create_rollout(
            artifact_version="v2.0.0",
            target_agent_ids=["agent_ro_1", "agent_ro_2", "agent_ro_3", "agent_ro_4"],
            batch_size=2,
            health_gate="STRICT",
        )
        assert plan.status == RolloutStatus.PENDING

        # Execute rollout with all probes returning HEALTHY
        completed_plan = coordinator.execute_rollout(
            plan.rollout_id,
            health_probe_fn=lambda aid: HealthState.HEALTHY,
        )
        assert completed_plan.status == RolloutStatus.COMPLETED
        for t in completed_plan.targets:
            assert t.current_version == "v2.0.0"
            assert t.status == "COMPLETED"

        # Verify generation fencing: old generation commits rejected
        with pytest.raises(StaleGenerationCommitError):
            coordinator.verify_runtime_generation("agent_ro_1", submitted_gen=1)

        # Current generation accepted
        assert coordinator.verify_runtime_generation(
            "agent_ro_1", submitted_gen=completed_plan.runtime_generation
        )


def test_failed_rollout_health_gate_and_rollback() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_rollout_fail", base_dir=tmpdir)
        for i in range(1, 4):
            controller.register_agent(
                AgentInstance(
                    agent_instance_id=f"agent_f_{i}",
                    fleet_id="f_rollout_fail",
                    status=AgentInstanceStatus.READY,
                )
            )

        coordinator = RolloutCoordinator(controller)
        plan = coordinator.create_rollout(
            artifact_version="v2.1.0-bad",
            target_agent_ids=["agent_f_1", "agent_f_2", "agent_f_3"],
            batch_size=1,
            rollback_policy="AUTOMATIC",
        )

        # Health probe fails on agent_f_1
        def probe(aid: str) -> HealthState:
            return HealthState.UNHEALTHY if aid == "agent_f_1" else HealthState.HEALTHY

        res = coordinator.execute_rollout(plan.rollout_id, health_probe_fn=probe)
        assert res.status == RolloutStatus.ROLLED_BACK
        assert res.rollback_revision is not None
        assert "agent_f_1" in res.rollback_reason
