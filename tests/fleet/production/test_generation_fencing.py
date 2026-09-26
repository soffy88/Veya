"""PQ §9: Generation Fencing and Stale Commit Denial.

Invariants:
- STALE_FLEET_COMMIT_ACCEPTED=0
- STALE_AGENT_COMMIT_ACCEPTED=0
- GENERATION_FENCE=PASS
"""

from __future__ import annotations

import tempfile

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    AgentLease,
    FleetController,
    MissionMigration,
    PlacementDecision,
    ResourceReservation,
    ResourceType,
    StaleGenerationError,
)


def test_generation_fencing_qualification() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_gen_fence", base_dir=tmpdir)
        controller.resources.register_pool("core_pool", ResourceType.CPU, capacity=10.0)

        agent = AgentInstance(
            agent_instance_id="agent_gen_1",
            fleet_id="f_gen_fence",
            generation=1,
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # 1. Advance fleet generation from 1 to 2
        fleet_gen = controller.advance_generation()
        assert fleet_gen == 2

        # 2. Deny placement commit from stale fleet generation 1
        stale_plc = PlacementDecision(
            placement_id="plc_stale_gen",
            mission_id="m_stale_1",
            agent_instance_id="agent_gen_1",
            runtime_id="rt_1",
            reason="Stale generation",
            generation=1,  # 1 < 2
        )
        with pytest.raises(StaleGenerationError):
            controller.registry.save_placement(stale_plc)

        # 3. Deny lease commit from stale fleet generation 1
        stale_lease = AgentLease(
            lease_id="ls_stale_gen",
            agent_instance_id="agent_gen_1",
            mission_id="m_stale_1",
            holder_generation=1,
        )
        with pytest.raises(StaleGenerationError):
            controller.registry.save_lease(stale_lease, expected_fleet_generation=1)

        # 4. Deny migration commit from stale fleet generation 1
        stale_mig = MissionMigration(
            migration_id="mig_stale_gen",
            mission_id="m_stale_1",
            source_agent_instance_id="agent_gen_1",
            target_agent_instance_id="agent_gen_1",
            reason="Stale migration",
            state_snapshot_ref="snap_stale",
        )
        with pytest.raises(StaleGenerationError):
            controller.registry.save_migration(stale_mig, expected_fleet_generation=1)

        # 5. Deny reservation commit from stale fleet generation 1
        stale_res = ResourceReservation(
            reservation_id="res_stale_gen",
            mission_id="m_stale_1",
            agent_instance_id="agent_gen_1",
            pool_id="core_pool",
            quantity=1.0,
        )
        with pytest.raises(StaleGenerationError):
            controller.registry.save_reservation(stale_res, expected_fleet_generation=1)

        # 6. Advance agent generation from 1 to 2 and deny stale agent commits
        agent_gen = controller.registry.advance_agent_generation("agent_gen_1")
        assert agent_gen == 2

        # Stale agent update must raise StaleGenerationError
        with pytest.raises(StaleGenerationError):
            controller.registry.update_agent_status(
                "agent_gen_1", AgentInstanceStatus.BUSY, generation=1
            )
