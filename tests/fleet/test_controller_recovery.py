"""Tests for Controller Crash Recovery and Stale Controller Fencing (E2E-E, E2E-F)."""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementDecision,
    PlacementRequest,
    StaleGenerationError,
)


def test_controller_crash_recovery_e2e_e() -> None:
    """E2E-E: Controller process crash/restart recovers queue, placements, and active leases without state loss (FLEET_STATE_LOSS=0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Controller 1 instance
        c1 = FleetController(fleet_id="f_crash", base_dir=tmpdir, global_max_missions=1)
        agent = AgentInstance(
            agent_instance_id="agent_c1",
            fleet_id="f_crash",
            capability_profile=["python"],
            status=AgentInstanceStatus.READY,
        )
        c1.register_agent(agent)

        # Place 1 mission
        req1 = PlacementRequest(
            mission_id="m_c1_active",
            principal_id="user_c",
            required_capabilities=["python"],
        )
        plc1, _ = c1.admit_and_schedule(req1)
        assert plc1 is not None

        # Enqueue 1 mission due to global capacity limit
        req2 = PlacementRequest(
            mission_id="m_c1_queued",
            principal_id="user_c",
            required_capabilities=["python"],
        )
        c1.admit_and_schedule(req2)

        # Simulate sudden SIGKILL: discard c1 in-memory instance without graceful shutdown
        del c1

        # Controller 2 starts on same base_dir (recovery)
        c2 = FleetController(fleet_id="f_crash", base_dir=tmpdir, global_max_missions=1)

        # Invariant checks: queue restored, placement intact, lease active
        restored_plc = c2.registry.find_active_placement_for_mission("m_c1_active")
        assert restored_plc is not None
        assert restored_plc.placement_id == plc1.placement_id

        restored_queue = c2.registry.list_queue()
        assert len(restored_queue) == 1
        assert restored_queue[0].mission_id == "m_c1_queued"

        restored_lease = c2.registry.find_active_lease_for_mission("m_c1_active")
        assert restored_lease is not None


def test_stale_controller_fencing_e2e_f() -> None:
    """E2E-F: Old generation controller commits are rejected when generation advances (STALE_FLEET_COMMIT_ACCEPTED=0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        c = FleetController(fleet_id="f_fencing", base_dir=tmpdir)
        old_gen = c.registry.get_fleet().generation
        assert old_gen == 1

        # Advance controller generation to 2 (e.g. new leader elected)
        new_gen = c.advance_generation()
        assert new_gen == 2

        # Stale controller tries to commit a placement under generation 1
        stale_placement = PlacementDecision(
            placement_id="plc_stale_leader",
            mission_id="m_stale_leader",
            agent_instance_id="agent_any",
            runtime_id="rt_any",
            reason="Stale generation attempt",
            generation=old_gen,  # generation 1 < current 2
        )
        with pytest.raises(StaleGenerationError):
            c.registry.save_placement(stale_placement)
