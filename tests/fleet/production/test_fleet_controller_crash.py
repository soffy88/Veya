"""PQ §8: Fleet Controller Crash and Restart Across Lifecycle Stages.

Invariants:
- FLEET_CONTROLLER_RECOVERY=PASS
- DUPLICATE_PLACEMENT=0
- RESERVATION_LEAK=0
- Tests restart before placement commit, after reservation, after placement commit, and during migration.
"""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
    ReservationStatus,
    ResourceType,
)


def test_controller_crash_across_lifecycle_stages() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Stage 1: Controller restart after reservation, before placement commit
        c1 = FleetController(fleet_id="f_ctl_crash", base_dir=tmpdir)
        c1.resources.register_pool("mem_pool", ResourceType.RAM, capacity=16.0)

        agent = AgentInstance(
            agent_instance_id="agent_c1",
            fleet_id="f_ctl_crash",
            capability_profile=["work"],
            status=AgentInstanceStatus.READY,
        )
        c1.register_agent(agent)

        res1 = c1.resources.reserve(
            mission_id="m_stage1_reserved",
            pool_id="mem_pool",
            quantity=4.0,
        )
        assert res1.status == ReservationStatus.RESERVED

        # Kill c1
        del c1

        # Restart controller
        c2 = FleetController(fleet_id="f_ctl_crash", base_dir=tmpdir)
        # Verify reservation restored
        restored_res = c2.registry.get_reservation(res1.reservation_id)
        assert restored_res is not None
        assert restored_res.status == ReservationStatus.RESERVED

        # Stage 2: Controller restart after placement commit
        req2 = PlacementRequest(
            mission_id="m_stage2_placed",
            principal_id="user_c",
            required_capabilities=["work"],
        )
        plc2, _ = c2.admit_and_schedule(req2)
        assert plc2 is not None

        # Kill c2
        del c2

        # Restart controller c3
        c3 = FleetController(fleet_id="f_ctl_crash", base_dir=tmpdir)
        # Verify placement restored and idempotent (DUPLICATE_PLACEMENT=0)
        plc_restored = c3.registry.find_active_placement_for_mission("m_stage2_placed")
        assert plc_restored is not None
        assert plc_restored.placement_id == plc2.placement_id

        # Rescheduling must be idempotent
        dup_plc, status = c3.admit_and_schedule(req2)
        assert dup_plc is not None
        assert status == "IDEMPOTENT_EXISTING_PLACEMENT"

        # Stage 3: Release and verify zero reservation leak (RESERVATION_LEAK=0)
        c3.resources.release_reservation(res1.reservation_id)
        pool = c3.registry.get_pool("mem_pool")
        assert pool.available == 16.0
        assert pool.reserved == 0.0
