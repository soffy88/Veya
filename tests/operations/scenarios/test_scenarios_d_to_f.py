"""Qualification Scenarios D, E, F for Operations V1 (spec §53-§55).

Scenario D: Rolling Upgrade (>= 5 agents, zero mission loss, generation fencing)
Scenario E: Failed Rollout (health gate breach, halt)
Scenario F: Rollback (version lineage, mission protection)
"""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)
from veya.operations import (
    HealthState,
    OperationsController,
    RolloutStatus,
    StaleGenerationCommitError,
)


def test_scenario_d_rolling_upgrade() -> None:
    """Scenario D: 5 agents rolling upgrade with concurrent mission submission (spec §53)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fleet = FleetController(fleet_id="f_scen_d", base_dir=f"{tmpdir}/fleet")
        agent_ids = [f"agent_d_{i}" for i in range(1, 6)]
        for aid in agent_ids:
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=aid,
                    fleet_id="f_scen_d",
                    capability_profile=["compute"],
                    status=AgentInstanceStatus.READY,
                )
            )

        ops = OperationsController(base_dir=f"{tmpdir}/ops", fleet_controller=fleet)

        # Submit active mission before rollout
        plc, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_d_active",
                principal_id="user_d",
                required_capabilities=["compute"],
            )
        )
        assert plc is not None

        # Create rollout plan
        plan = ops.rollouts.create_rollout(
            artifact_version="v2.0.0",
            target_agent_ids=agent_ids,
            batch_size=2,
            health_gate="STRICT",
        )

        # Execute rollout
        completed_plan = ops.rollouts.execute_rollout(
            plan.rollout_id,
            health_probe_fn=lambda aid: HealthState.HEALTHY,
        )
        assert completed_plan.status == RolloutStatus.COMPLETED

        # ZERO_MISSION_LOSS=YES: initial mission still active
        active_plc = fleet.registry.find_active_placement_for_mission("m_d_active")
        assert active_plc is not None
        assert active_plc.mission_id == "m_d_active"

        # STALE_RUNTIME_GENERATION_ACCEPTED=0: stale generation commits rejected
        with pytest.raises(StaleGenerationCommitError):
            ops.rollouts.verify_runtime_generation("agent_d_1", submitted_gen=1)

        assert ops.rollouts.verify_runtime_generation(
            "agent_d_1", submitted_gen=completed_plan.runtime_generation
        )


def test_scenario_e_failed_rollout() -> None:
    """Scenario E: Inject unhealthy batch, verify rollout halted (spec §54)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fleet = FleetController(fleet_id="f_scen_e", base_dir=f"{tmpdir}/fleet")
        for i in range(1, 4):
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=f"agent_e_{i}",
                    fleet_id="f_scen_e",
                    capability_profile=["compute"],
                    status=AgentInstanceStatus.READY,
                )
            )

        ops = OperationsController(base_dir=f"{tmpdir}/ops", fleet_controller=fleet)

        plan = ops.rollouts.create_rollout(
            artifact_version="v2.1.0-defect",
            target_agent_ids=["agent_e_1", "agent_e_2", "agent_e_3"],
            batch_size=1,
            rollback_policy="MANUAL",
        )

        # Health probe fails on agent_e_1
        def probe(aid: str) -> HealthState:
            return HealthState.UNHEALTHY if aid == "agent_e_1" else HealthState.HEALTHY

        halted_plan = ops.rollouts.execute_rollout(plan.rollout_id, health_probe_fn=probe)
        assert halted_plan.status == RolloutStatus.HALTED  # ROLLOUT_HALTED=YES
        # agent_e_2 and agent_e_3 were never upgraded (UNHEALTHY_BATCH_PROPAGATED=NO)
        assert halted_plan.targets[1].status == "PENDING"
        assert halted_plan.targets[2].status == "PENDING"


def test_scenario_f_rollback() -> None:
    """Scenario F: Execute rollback on failed rollout, verifying version lineage (spec §55)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fleet = FleetController(fleet_id="f_scen_f", base_dir=f"{tmpdir}/fleet")
        for i in range(1, 4):
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=f"agent_f_{i}",
                    fleet_id="f_scen_f",
                    capability_profile=["compute"],
                    status=AgentInstanceStatus.READY,
                )
            )

        ops = OperationsController(base_dir=f"{tmpdir}/ops", fleet_controller=fleet)

        plan = ops.rollouts.create_rollout(
            artifact_version="v2.5.0-flaky",
            target_agent_ids=["agent_f_1", "agent_f_2", "agent_f_3"],
            batch_size=1,
            rollback_policy="AUTOMATIC",
        )

        # Probing failure triggers automatic rollback
        def probe(aid: str) -> HealthState:
            return HealthState.UNHEALTHY if aid == "agent_f_1" else HealthState.HEALTHY

        res = ops.rollouts.execute_rollout(plan.rollout_id, health_probe_fn=probe)
        assert res.status == RolloutStatus.ROLLED_BACK
        assert res.rollback_revision is not None
        assert res.rollback_revision.artifact_version == "v1.0.0"
        for t in res.targets:
            assert t.status == "ROLLED_BACK"
            assert t.current_version == "v1.0.0"
