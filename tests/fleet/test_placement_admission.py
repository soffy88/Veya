"""Tests for Admission, Scheduling, Concurrent Placements, and Saturation (AF-P1, E2E-A, E2E-B)."""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def test_concurrent_missions_e2e_a() -> None:
    """E2E-A: At least 10 concurrent missions across >= 3 agents with unique placement."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_concurrent", base_dir=tmpdir, global_max_missions=50
        )

        # Register 3 agents
        for i in range(1, 4):
            agent = AgentInstance(
                agent_instance_id=f"agent_{i}",
                fleet_id="f_concurrent",
                capability_profile=["python", "bash"],
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        # Submit 10 concurrent mission placement requests
        placements = []
        for m_idx in range(1, 11):
            req = PlacementRequest(
                mission_id=f"mission_{m_idx}",
                principal_id=f"user_{m_idx % 2}",
                required_capabilities=["python"],
            )
            decision, status = controller.admit_and_schedule(req)
            assert decision is not None, f"Mission {m_idx} failed admission: {status}"
            assert decision.status == "ACTIVE"
            placements.append(decision)

        # Assertions
        assert len(placements) == 10
        assigned_agents = {p.agent_instance_id for p in placements}
        assert len(assigned_agents) >= 3, "Missions must be spread across all 3 agents"

        # Idempotency check: rescheduling same mission returns existing placement (DUPLICATE_PLACEMENT=0)
        req_dup = PlacementRequest(
            mission_id="mission_1",
            principal_id="user_1",
            required_capabilities=["python"],
        )
        dup_decision, dup_status = controller.admit_and_schedule(req_dup)
        assert dup_decision is not None
        assert dup_decision.placement_id == placements[0].placement_id
        assert dup_status == "IDEMPOTENT_EXISTING_PLACEMENT"


def test_saturation_backpressure_e2e_b() -> None:
    """E2E-B: Saturation defers new missions without failing them (SATURATION_FALSE_FAILURE=0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Limit global capacity to 3
        controller = FleetController(fleet_id="f_sat", base_dir=tmpdir, global_max_missions=3)
        agent = AgentInstance(
            agent_instance_id="agent_solo",
            fleet_id="f_sat",
            capability_profile=["compute"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # Place 3 missions (saturating the fleet)
        for i in range(1, 4):
            req = PlacementRequest(
                mission_id=f"m_sat_{i}",
                principal_id="alice",
                required_capabilities=["compute"],
            )
            d, _s = controller.admit_and_schedule(req)
            assert d is not None

        # 4th mission must DEFER, NOT reject or fail
        req_overflow = PlacementRequest(
            mission_id="m_sat_overflow",
            principal_id="alice",
            required_capabilities=["compute"],
        )
        d_over, s_over = controller.admit_and_schedule(req_overflow)
        assert d_over is None
        assert "DEFERRED" in s_over  # SATURATION_FALSE_FAILURE=0

        # Verify entry is enqueued
        queue = controller.registry.list_queue()
        assert len(queue) == 1
        assert queue[0].mission_id == "m_sat_overflow"


def test_capability_matching_filter() -> None:
    """Capability filtering guarantees invalid placement is never made (CAPABILITY_INVALID_PLACEMENT=0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_cap", base_dir=tmpdir)
        # Agent has only python
        agent = AgentInstance(
            agent_instance_id="agent_py",
            fleet_id="f_cap",
            capability_profile=["python"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # Request requires rust
        req = PlacementRequest(
            mission_id="m_rust",
            principal_id="bob",
            required_capabilities=["rust"],
        )
        decision, status = controller.admit_and_schedule(req)
        # Must be rejected because no agent in fleet can ever satisfy rust
        assert decision is None
        assert "REJECTED" in status
