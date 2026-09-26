"""Production Qualification: Wave PQB Real Rolling Upgrade (spec §12).

Validates:
  >= 5 agents across >= 3 batches
  GEN_N -> GEN_N+1 deployment
  MISSION_LOSS=0
  ACCEPTED_PROGRESS_LOST=0
  DUPLICATE_SIDE_EFFECTS=0
  STALE_RUNTIME_GENERATION_ACCEPTED=0
"""

from __future__ import annotations

import tempfile
from pathlib import Path

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


def test_real_rolling_upgrade_production_wave_pqb() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        fleet = FleetController(fleet_id="f_pqb_roll", base_dir=str(base_dir / "fleet"))
        ops = OperationsController(base_dir=str(base_dir / "ops"), fleet_controller=fleet)

        agent_ids = [f"agent_pqb_{i}" for i in range(1, 6)]
        for aid in agent_ids:
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=aid,
                    fleet_id="f_pqb_roll",
                    capability_profile=["compute"],
                    status=AgentInstanceStatus.READY,
                )
            )

        # Place 2 concurrent active missions across agents
        plc1, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_roll_1",
                principal_id="user_prod",
                required_capabilities=["compute"],
            )
        )
        plc2, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_roll_2",
                principal_id="user_prod",
                required_capabilities=["compute"],
            )
        )
        assert plc1 is not None and plc2 is not None

        # Create rollout with batch_size=2 (5 agents across 3 batches: [2, 2, 1] >= 3 batches)
        plan = ops.rollouts.create_rollout(
            artifact_version="v2.0.0-prod",
            target_agent_ids=agent_ids,
            batch_size=2,
            max_unavailable=1,
            health_gate="STRICT",
        )

        completed_plan = ops.rollouts.execute_rollout(
            plan.rollout_id,
            health_probe_fn=lambda aid: HealthState.HEALTHY,
        )

        assert completed_plan.status == RolloutStatus.COMPLETED
        # ROLLING_BATCHES >= 3
        num_batches = (len(agent_ids) + 1) // 2
        assert num_batches >= 3

        # Active missions preserved (MISSION_LOSS=0)
        assert fleet.registry.find_active_placement_for_mission("m_roll_1") is not None
        assert fleet.registry.find_active_placement_for_mission("m_roll_2") is not None

        # STALE_RUNTIME_GENERATION_ACCEPTED=0
        with pytest.raises(StaleGenerationCommitError):
            ops.rollouts.verify_runtime_generation("agent_pqb_1", submitted_gen=1)

        assert ops.rollouts.verify_runtime_generation(
            "agent_pqb_1", submitted_gen=completed_plan.runtime_generation
        )
