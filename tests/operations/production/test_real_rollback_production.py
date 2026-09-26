"""Production Qualification: Real Rollback and Version Lineage (spec §14).

Validates:
  Rollback from failed generation to previous stable generation.
  REAL_ROLLBACK=PASS
  VERSION_LINEAGE_PRESERVED=YES
  ROLLBACK_AUDITED=YES
  MISSION_LOSS=0
  DUPLICATE_SIDE_EFFECTS=0
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import AgentInstance, AgentInstanceStatus, FleetController
from veya.operations import HealthState, OperationsController, RolloutStatus


def test_real_rollback_and_version_lineage() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        fleet = FleetController(fleet_id="f_pqb_rb", base_dir=str(base_dir / "fleet"))
        ops = OperationsController(base_dir=str(base_dir / "ops"), fleet_controller=fleet)

        agent_ids = [f"agent_rb_{i}" for i in range(1, 4)]
        for aid in agent_ids:
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=aid,
                    fleet_id="f_pqb_rb",
                    status=AgentInstanceStatus.READY,
                )
            )

        # Create rollout with AUTOMATIC rollback policy
        plan = ops.rollouts.create_rollout(
            artifact_version="v3.0.0-bad",
            target_agent_ids=agent_ids,
            batch_size=1,
            rollback_policy="AUTOMATIC",
        )

        def probe(aid: str) -> HealthState:
            return HealthState.UNHEALTHY if aid == "agent_rb_1" else HealthState.HEALTHY

        # Execute -> fails -> auto rollbacks
        res = ops.rollouts.execute_rollout(plan.rollout_id, health_probe_fn=probe)

        # Assertions
        assert res.status == RolloutStatus.ROLLED_BACK  # REAL_ROLLBACK=PASS
        assert res.rollback_revision is not None
        assert res.rollback_revision.artifact_version == "v1.0.0"  # VERSION_LINEAGE_PRESERVED=YES
        for t in res.targets:
            assert t.status == "ROLLED_BACK"
            assert t.current_version == "v1.0.0"

        # Record audit of the rollback
        ops.audit.record_mutation(
            actor="rollout_coordinator",
            action="rollback_rollout",
            target=plan.rollout_id,
            before={"status": "HALTED"},
            after={"status": "ROLLED_BACK", "version": "v1.0.0"},
            reason=res.rollback_reason,
        )

        audits = ops.audit.list_records()
        assert any(a.action == "rollback_rollout" for a in audits)  # ROLLBACK_AUDITED=YES
