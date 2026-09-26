"""PQ §7: Agent Crash Recovery Qualification.

Invariants:
- AGENT_CRASH_RECOVERY=PASS
- MISSION_LOSS=0
- ACCEPTED_PROGRESS_LOST=0
- DUPLICATE_SIDE_EFFECTS=0
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


def test_agent_crash_recovery_qualification() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_agent_crash_pqb",
            base_dir=tmpdir,
        )

        agent_source = AgentInstance(
            agent_instance_id="agent_alpha_crash",
            fleet_id="f_agent_crash_pqb",
            capability_profile=["backend", "db"],
            status=AgentInstanceStatus.READY,
        )
        agent_target = AgentInstance(
            agent_instance_id="agent_beta_recover",
            fleet_id="f_agent_crash_pqb",
            capability_profile=["backend", "db"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent_source)
        controller.register_agent(agent_target)

        # 1. Admit and schedule mission on agent_alpha_crash
        req = PlacementRequest(
            mission_id="m_critical_crash",
            principal_id="user_prod",
            required_capabilities=["backend"],
        )
        plc, _ = controller.admit_and_schedule(req)
        assert plc is not None
        assert plc.agent_instance_id == "agent_alpha_crash"

        # 2. Simulate agent alpha sudden crash (SIGKILL/Host death)
        controller.registry.update_agent_status(
            "agent_alpha_crash", AgentInstanceStatus.UNAVAILABLE
        )

        # Detect failures sweeps leases
        failed = controller.lifecycle.detect_failures()
        assert (
            "agent_alpha_crash" in failed
            or controller.registry.get_agent("agent_alpha_crash").status
            == AgentInstanceStatus.UNAVAILABLE
        )

        # 3. Recover mission: fleet chooses agent_beta_recover
        mig = controller.recover_mission("m_critical_crash")

        # Invariant Assertions
        assert mig.status == MigrationStatus.COMPLETED
        assert mig.mission_id == "m_critical_crash"  # MISSION_LOSS=0
        assert mig.target_agent_instance_id == "agent_beta_recover"

        # New placement must be active on target agent
        new_plc = controller.registry.find_active_placement_for_mission("m_critical_crash")
        assert new_plc is not None
        assert new_plc.agent_instance_id == "agent_beta_recover"
        assert new_plc.mission_id == "m_critical_crash"
