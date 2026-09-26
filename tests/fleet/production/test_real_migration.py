"""PQ §11: Real Mission Migration Preserving Identity and Progress.

Invariants:
- MISSION_ID_CHANGED=0
- GOAL_RUN_ID_CHANGED=0
- ACCEPTED_PROGRESS_LOST=0
- MIGRATION=PASS
"""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    MigrationStatus,
    PlacementRequest,
)


def test_real_migration_identity_and_progress_preservation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_migration_pqb", base_dir=tmpdir)

        agent_source = AgentInstance(
            agent_instance_id="agent_src_1",
            fleet_id="f_migration_pqb",
            runtime_id="rt_src",
            capability_profile=["compute"],
            status=AgentInstanceStatus.READY,
        )
        agent_dest = AgentInstance(
            agent_instance_id="agent_dst_1",
            fleet_id="f_migration_pqb",
            runtime_id="rt_dst",
            capability_profile=["compute"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent_source)
        controller.register_agent(agent_dest)

        # Place mission on source
        req = PlacementRequest(
            mission_id="m_mig_stable_id",
            principal_id="user_mig",
            required_capabilities=["compute"],
        )
        plc_src, _ = controller.admit_and_schedule(req)
        assert plc_src is not None
        assert plc_src.agent_instance_id == "agent_src_1"

        # State snapshot representing durable progress
        snapshot_ref = "snap_m_mig_stable_id_chk3"
        snapshot_details = {
            "mission_id": "m_mig_stable_id",
            "goal_run_id": "gr_m_mig_stable_id_01",
            "accepted_progress": ["Milestone 1: Init", "Milestone 2: Build"],
            "decision_history_count": 5,
        }

        # Initiate and execute migration
        mig = controller.migrations.initiate_migration(
            mission_id="m_mig_stable_id",
            target_agent_instance_id="agent_dst_1",
            reason="Planned maintenance migration",
            state_snapshot_ref=snapshot_ref,
            details=snapshot_details,
        )
        completed_mig = controller.migrations.execute_migration(mig.migration_id)

        # Invariant Assertions
        assert completed_mig.status == MigrationStatus.COMPLETED
        assert completed_mig.mission_id == "m_mig_stable_id"  # MISSION_ID_CHANGED=0
        assert (
            completed_mig.details["goal_run_id"] == "gr_m_mig_stable_id_01"
        )  # GOAL_RUN_ID_CHANGED=0
        assert len(completed_mig.details["accepted_progress"]) == 2  # ACCEPTED_PROGRESS_LOST=0

        # Destination placement
        active_plc = controller.registry.find_active_placement_for_mission("m_mig_stable_id")
        assert active_plc is not None
        assert active_plc.agent_instance_id == "agent_dst_1"
        assert active_plc.runtime_id == "rt_dst"
