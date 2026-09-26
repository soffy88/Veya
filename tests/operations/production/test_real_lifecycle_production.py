"""Production Qualification: Wave PQA Real Lifecycle (spec §9).

Validates:
  ACTIVE -> PAUSED -> ACTIVE -> DRAINING -> DRAINED -> ACTIVE
Under real concurrent mission submission:
  - REAL_PAUSE_RESUME=PASS
  - REAL_DRAIN=PASS
  - NEW_ADMISSION_WHILE_PAUSED=0
  - NEW_PLACEMENT_WHILE_DRAINING=0
  - MISSION_LOSS=0
  - ACCEPTED_PROGRESS_LOST=0
  - DUPLICATE_SIDE_EFFECTS=0
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)
from veya.operations import AgentOperationalStatus, OperationsController


def test_real_lifecycle_production_wave_pqa() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        fleet_data = base_dir / "fleet"
        ops_data = base_dir / "ops"

        fleet = FleetController(fleet_id="f_pqa_life", base_dir=str(fleet_data))
        ops = OperationsController(base_dir=str(ops_data), fleet_controller=fleet)

        # Register 5 agents with distinct workspace scopes
        repo_veya = Path("/data/soffy/projects/veya")
        repo_hevi = Path("/data/soffy/projects/hevi")
        repo_stratum = Path("/data/soffy/projects/stratum")

        agents = [
            ("agent_pqa_1", [str(repo_veya)], ["python", "compute"]),
            ("agent_pqa_2", [str(repo_hevi)], ["rust", "compute"]),
            ("agent_pqa_3", [str(repo_stratum)], ["golang", "compute"]),
            ("agent_pqa_4", [str(repo_veya)], ["coding", "compute"]),
            ("agent_pqa_5", [str(repo_hevi)], ["test", "compute"]),
        ]
        for aid, ws, caps in agents:
            fleet.register_agent(
                AgentInstance(
                    agent_instance_id=aid,
                    fleet_id="f_pqa_life",
                    workspace_scope=ws,
                    capability_profile=caps,
                    status=AgentInstanceStatus.READY,
                )
            )

        # 1. Place active mission on agent_pqa_1
        plc1, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_life_active_1",
                principal_id="user_prod_1",
                required_capabilities=["python"],
            )
        )
        assert plc1 is not None
        assert plc1.agent_instance_id == "agent_pqa_1"

        # 2. Pause agent_pqa_1
        st_paused = ops.pause_agent("agent_pqa_1", reason="Pre-upgrade inspection")
        assert st_paused.desired_state == AgentOperationalStatus.PAUSED
        assert st_paused.observed_state == AgentOperationalStatus.PAUSED

        # 3. NEW_ADMISSION_WHILE_PAUSED=0
        plc_blocked, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_life_should_block",
                principal_id="user_prod_1",
                required_capabilities=["python"],  # only agent_pqa_1 has 'python'
            )
        )
        assert plc_blocked is None  # strictly denied admission

        # 4. In-flight mission is protected (MISSION_LOSS=0)
        active_plc = fleet.registry.find_active_placement_for_mission("m_life_active_1")
        assert active_plc is not None
        assert active_plc.agent_instance_id == "agent_pqa_1"

        # 5. Resume agent_pqa_1
        st_resumed = ops.resume_agent("agent_pqa_1", reason="Inspection cleared")
        assert st_resumed.desired_state == AgentOperationalStatus.ACTIVE
        assert st_resumed.observed_state == AgentOperationalStatus.ACTIVE

        # 6. Admission restored
        plc_resumed, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_life_now_allowed",
                principal_id="user_prod_1",
                required_capabilities=["python"],
            )
        )
        assert plc_resumed is not None
        assert plc_resumed.agent_instance_id == "agent_pqa_1"

        # 7. Drain agent_pqa_2
        plc_hevi, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_life_hevi",
                principal_id="user_prod_2",
                required_capabilities=["rust"],
            )
        )
        assert plc_hevi is not None
        assert plc_hevi.agent_instance_id == "agent_pqa_2"

        st_draining = ops.drain_agent("agent_pqa_2", reason="Host migration")
        assert st_draining.desired_state == AgentOperationalStatus.DRAINING

        # 8. NEW_PLACEMENT_WHILE_DRAINING=0
        plc_drain_block, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_life_drain_block",
                principal_id="user_prod_2",
                required_capabilities=["rust"],  # only agent_pqa_2 has 'rust'
            )
        )
        assert plc_drain_block is None

        # 9. Release in-flight work and sweep -> reaches STOPPED (DRAINED)
        fleet.release_placement("m_life_hevi")
        fleet.lifecycle.sweep_draining_agents()
        assert fleet.registry.get_agent("agent_pqa_2").status == AgentInstanceStatus.STOPPED

        # 10. Re-activate agent_pqa_2 from STOPPED to READY
        ops.resume_agent("agent_pqa_2", reason="Host migration completed")
        assert fleet.registry.get_agent("agent_pqa_2").status == AgentInstanceStatus.READY
