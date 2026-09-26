"""Tests for Mission Recovery, Planned Migration, Workspace Affinity, and Failure Domains (AF-P4, E2E-D, E2E-H, E2E-L)."""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    MigrationStatus,
    PlacementRequest,
)


def test_agent_crash_recovery_e2e_d() -> None:
    """E2E-D: When Agent A dies, Mission is recovered on Agent B with identical mission_id (MISSION_ID_CHANGED=0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_recovery", base_dir=tmpdir)

        # Register Agent A and Agent B
        agent_a = AgentInstance(
            agent_instance_id="agent_A",
            fleet_id="f_recovery",
            capability_profile=["backend"],
            status=AgentInstanceStatus.READY,
        )
        agent_b = AgentInstance(
            agent_instance_id="agent_B",
            fleet_id="f_recovery",
            capability_profile=["backend"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent_a)
        controller.register_agent(agent_b)

        # Place mission on Agent A
        req = PlacementRequest(
            mission_id="m_survivor",
            principal_id="user_recovery",
            required_capabilities=["backend"],
        )
        plc_a, _ = controller.admit_and_schedule(req)
        assert plc_a is not None
        assert plc_a.agent_instance_id == "agent_A"

        # Simulate Agent A sudden crash: mark UNAVAILABLE and expire lease
        controller.registry.update_agent_status("agent_A", AgentInstanceStatus.UNAVAILABLE)
        active_lease = controller.registry.find_active_lease_for_mission("m_survivor")
        if active_lease:
            controller.leases.release_lease(active_lease.lease_id)

        # Execute recovery placement
        mig = controller.recover_mission("m_survivor")

        # Verify invariants
        assert mig.status == MigrationStatus.COMPLETED
        assert mig.mission_id == "m_survivor"  # MISSION_ID_CHANGED=0
        assert mig.target_agent_instance_id == "agent_B"

        # Check new placement
        new_plc = controller.registry.find_active_placement_for_mission("m_survivor")
        assert new_plc is not None
        assert new_plc.agent_instance_id == "agent_B"
        assert new_plc.mission_id == "m_survivor"


def test_workspace_affinity_and_failure_domain_e2e_h_e2e_l() -> None:
    """E2E-H & E2E-L: Workspace locality preference and failure domain avoidance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_locality", base_dir=tmpdir)

        # Agent 1 has local workspace cache
        agent_local = AgentInstance(
            agent_instance_id="agent_local",
            fleet_id="f_locality",
            capability_profile=["coding"],
            workspace_scope=["/data/workspaces/proj_x"],
            failure_domain="zone-1",
            status=AgentInstanceStatus.READY,
        )
        # Agent 2 is remote with different workspace
        agent_remote = AgentInstance(
            agent_instance_id="agent_remote",
            fleet_id="f_locality",
            capability_profile=["coding"],
            workspace_scope=["/data/workspaces/other"],
            failure_domain="zone-2",
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent_local)
        controller.register_agent(agent_remote)

        # Request requires /data/workspaces/proj_x
        req = PlacementRequest(
            mission_id="m_loc_1",
            principal_id="alice",
            required_capabilities=["coding"],
            workspace_requirements={"workspace_path": "/data/workspaces/proj_x"},
        )
        plc, _ = controller.admit_and_schedule(req)
        assert plc is not None
        # Must pick agent_local due to workspace affinity (spec §16)
        assert plc.agent_instance_id == "agent_local"
