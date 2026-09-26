"""Production Qualification: Failed Rollout Detection and Propagation Halt (spec §13).

Validates:
  Deterministic failure injection in a rollout batch.
  FAILED_ROLLOUT_DETECTED=YES
  ROLLOUT_HALTED=YES
  NEXT_BATCH_STARTED=NO
  UNHEALTHY_GENERATION_PROPAGATED=NO
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import AgentInstance, AgentInstanceStatus, FleetController
from veya.operations import HealthState, OperationsController, RolloutStatus


def test_failed_rollout_detection_and_halt() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        fleet = FleetController(fleet_id="f_pqb_fail", base_dir=str(base_dir / "fleet"))
        ops = OperationsController(base_dir=str(base_dir / "ops"), fleet_controller=fleet)

        agent_ids = [f"agent_rf_{i}" for i in range(1, 6)]
        for aid in agent_ids:
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=aid,
                    fleet_id="f_pqb_fail",
                    status=AgentInstanceStatus.READY,
                )
            )

        # Rollout across 5 agents, batch_size=2
        plan = ops.rollouts.create_rollout(
            artifact_version="v2.1.0-unstable",
            target_agent_ids=agent_ids,
            batch_size=2,
            rollback_policy="MANUAL",
        )

        # Inject failure on agent_rf_1 in first batch
        def probe(aid: str) -> HealthState:
            return HealthState.UNHEALTHY if aid == "agent_rf_1" else HealthState.HEALTHY

        halted_plan = ops.rollouts.execute_rollout(plan.rollout_id, health_probe_fn=probe)

        # Invariant Assertions
        assert halted_plan.status == RolloutStatus.HALTED  # ROLLOUT_HALTED=YES
        assert "agent_rf_1" in halted_plan.rollback_reason

        # NEXT_BATCH_STARTED=NO & UNHEALTHY_GENERATION_PROPAGATED=NO
        # Agents 3, 4, 5 in subsequent batches must remain untouched in PENDING status
        assert halted_plan.targets[2].status == "PENDING"
        assert halted_plan.targets[3].status == "PENDING"
        assert halted_plan.targets[4].status == "PENDING"
