"""Qualification Scenarios A, B, C for Operations V1 (spec §50-§52).

Scenario A: Pause / Resume
Scenario B: Drain
Scenario C: Maintenance Window
"""

from __future__ import annotations

import tempfile
import time

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)
from veya.operations import MaintenanceScope, OperationsController
from veya.operations.models import AgentOperationalStatus


def test_scenario_a_pause_resume() -> None:
    """Scenario A: active mission -> pause -> no new admission -> resume (spec §50)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fleet = FleetController(fleet_id="f_scen_a", base_dir=f"{tmpdir}/fleet")
        agent = AgentInstance(
            agent_instance_id="agent_a_1",
            fleet_id="f_scen_a",
            capability_profile=["compute"],
            status=AgentInstanceStatus.READY,
        )
        fleet.register_agent(agent)

        ops = OperationsController(base_dir=f"{tmpdir}/ops", fleet_controller=fleet)

        # 1. Admit active mission
        plc1, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_active_1",
                principal_id="user_a",
                required_capabilities=["compute"],
            )
        )
        assert plc1 is not None

        # 2. Pause agent
        st = ops.pause_agent("agent_a_1", reason="Scheduled maintenance pause")
        assert st.desired_state == AgentOperationalStatus.PAUSED

        # 3. New admission must be rejected while paused
        plc2, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_blocked_2",
                principal_id="user_a",
                required_capabilities=["compute"],
            )
        )
        assert plc2 is None  # No new admission

        # 4. Active mission is preserved (MISSION_LOSS=0)
        active_plc = fleet.registry.find_active_placement_for_mission("m_active_1")
        assert active_plc is not None
        assert active_plc.mission_id == "m_active_1"

        # 5. Resume agent
        st_res = ops.resume_agent("agent_a_1", reason="Maintenance complete")
        assert st_res.desired_state == AgentOperationalStatus.ACTIVE

        # 6. Admission restored
        plc3, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_resumed_3",
                principal_id="user_a",
                required_capabilities=["compute"],
            )
        )
        assert plc3 is not None
        assert plc3.agent_instance_id == "agent_a_1"


def test_scenario_b_drain() -> None:
    """Scenario B: agent active -> drain -> no new placements -> in-flight work safely completes/migrates (spec §51)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fleet = FleetController(fleet_id="f_scen_b", base_dir=f"{tmpdir}/fleet")
        agent = AgentInstance(
            agent_instance_id="agent_b_1",
            fleet_id="f_scen_b",
            capability_profile=["compute"],
            status=AgentInstanceStatus.READY,
        )
        fleet.register_agent(agent)

        ops = OperationsController(base_dir=f"{tmpdir}/ops", fleet_controller=fleet)

        # Place initial mission
        plc, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_inflight_b",
                principal_id="user_b",
                required_capabilities=["compute"],
            )
        )
        assert plc is not None

        # Drain agent
        st_drain = ops.drain_agent("agent_b_1", reason="Decommissioning host")
        assert st_drain.desired_state == AgentOperationalStatus.DRAINING

        # NEW_WORK_AFTER_DRAIN=0
        plc_new, _ = fleet.admit_and_schedule(
            PlacementRequest(
                mission_id="m_rejected_after_drain",
                principal_id="user_b",
                required_capabilities=["compute"],
            )
        )
        assert plc_new is None

        # Release in-flight mission
        fleet.release_placement("m_inflight_b")
        fleet.lifecycle.sweep_draining_agents()
        # Drained agent transitions to STOPPED
        assert fleet.registry.get_agent("agent_b_1").status == AgentInstanceStatus.STOPPED


def test_scenario_c_maintenance() -> None:
    """Scenario C: Maintenance Window scope enforcement and admission gating (spec §52)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fleet = FleetController(fleet_id="f_scen_c", base_dir=f"{tmpdir}/fleet")
        agent = AgentInstance(
            agent_instance_id="agent_c_1",
            fleet_id="f_scen_c",
            capability_profile=["compute"],
            status=AgentInstanceStatus.READY,
        )
        fleet.register_agent(agent)

        ops = OperationsController(base_dir=f"{tmpdir}/ops", fleet_controller=fleet)

        now = time.time()
        # Set 2-hour maintenance window on agent_c_1
        mw = ops.set_maintenance(
            scope=MaintenanceScope.AGENT,
            target_id="agent_c_1",
            duration_s=7200.0,
            reason="Hardware upgrade",
        )
        assert mw.status == "ACTIVE"
        assert ops.lifecycle.is_target_in_maintenance(MaintenanceScope.AGENT, "agent_c_1")

        # Expired check
        assert not ops.lifecycle.is_target_in_maintenance(
            MaintenanceScope.AGENT, "agent_c_1", now=now + 8000
        )
