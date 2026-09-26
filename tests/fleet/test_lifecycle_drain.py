"""Tests for Agent Lifecycle, Leases, and Planned Drain (AF-P3, E2E-G)."""

from __future__ import annotations

import tempfile
import time

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    LeaseConflictError,
    LeaseStatus,
)


def test_agent_lease_exclusivity_and_expiration() -> None:
    """Only one active agent lease per mission, and expired leases are swept (spec §10)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_lease", base_dir=tmpdir)
        agent = AgentInstance(
            agent_instance_id="agent_fast",
            fleet_id="f_lease",
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # Acquire initial lease with short 0.2s TTL
        lease = controller.leases.acquire_lease(
            agent_instance_id="agent_fast",
            mission_id="m_exclusive",
            ttl_s=0.2,
        )
        assert lease.status == LeaseStatus.ACTIVE

        # Attempt to acquire a second active lease for same mission without parallel flag (ONE_ACTIVE_AGENT_LEASE_PER_MISSION=1)
        with pytest.raises(LeaseConflictError):
            controller.leases.acquire_lease(
                agent_instance_id="agent_fast",
                mission_id="m_exclusive",
                allow_parallel=False,
            )

        # Wait for lease to expire
        time.sleep(0.25)
        expired_count = controller.leases.sweep_expired_leases()
        assert expired_count == 1

        # Now a new lease can be acquired for this mission
        lease2 = controller.leases.acquire_lease(
            agent_instance_id="agent_fast",
            mission_id="m_exclusive",
        )
        assert lease2.status == LeaseStatus.ACTIVE


def test_planned_agent_drain_e2e_g() -> None:
    """E2E-G: Planned drain stops new placements, allows existing to finish, and marks agent STOPPED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_drain", base_dir=tmpdir)
        agent = AgentInstance(
            agent_instance_id="agent_draining",
            fleet_id="f_drain",
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # Acquire a running lease on agent
        lease = controller.leases.acquire_lease(
            agent_instance_id="agent_draining",
            mission_id="m_active_drain",
        )

        # Initiate drain
        controller.drain_agent("agent_draining")
        updated_agent = controller.registry.get_agent("agent_draining")
        assert updated_agent.status == AgentInstanceStatus.DRAINING

        # Candidate filtering must reject draining agent for new missions
        candidates = controller.scheduler.find_candidates(controller.admission.registry.get_fleet())
        assert "agent_draining" not in [c.agent_instance_id for c in candidates]

        # Finish running work by releasing lease
        controller.leases.release_lease(lease.lease_id)
        controller.lifecycle.sweep_draining_agents()

        final_agent = controller.registry.get_agent("agent_draining")
        assert final_agent.status == AgentInstanceStatus.STOPPED
