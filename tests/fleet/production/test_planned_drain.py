"""PQ §10: Planned Agent Drain Qualification.

Invariants:
- NEW_PLACEMENT_TO_DRAINING_AGENT=0
- DRAIN=PASS
- MISSION_LOSS=0
"""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def test_planned_agent_drain_qualification() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_drain_pqb", base_dir=tmpdir)

        # Register Agent A and Agent B
        agent_a = AgentInstance(
            agent_instance_id="agent_a_drain",
            fleet_id="f_drain_pqb",
            capability_profile=["work"],
            status=AgentInstanceStatus.READY,
        )
        agent_b = AgentInstance(
            agent_instance_id="agent_b_healthy",
            fleet_id="f_drain_pqb",
            capability_profile=["work"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent_a)
        controller.register_agent(agent_b)

        # Place running mission on Agent A
        req1 = PlacementRequest(
            mission_id="m_drain_active",
            principal_id="user_drain",
            required_capabilities=["work"],
        )
        plc1, _ = controller.admit_and_schedule(req1)
        assert plc1 is not None

        # Mark Agent A as DRAINING
        controller.drain_agent("agent_a_drain")
        assert controller.registry.get_agent("agent_a_drain").status == AgentInstanceStatus.DRAINING

        # Submit new mission -> MUST NOT be placed on draining Agent A (NEW_PLACEMENT_TO_DRAINING_AGENT=0)
        req2 = PlacementRequest(
            mission_id="m_drain_subsequent",
            principal_id="user_drain",
            required_capabilities=["work"],
        )
        plc2, _ = controller.admit_and_schedule(req2)
        assert plc2 is not None
        assert plc2.agent_instance_id == "agent_b_healthy"

        # Active mission completes on Agent A
        controller.release_placement("m_drain_active")
        controller.lifecycle.sweep_draining_agents()

        # Draining agent with 0 leases must reach STOPPED status
        assert controller.registry.get_agent("agent_a_drain").status == AgentInstanceStatus.STOPPED
