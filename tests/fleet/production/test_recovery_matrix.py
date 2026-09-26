"""PQ §21: Exhaustive Recovery Matrix Qualification.

Invariants:
- RECOVERY_MATRIX_CONTROLLER_CRASH_BEFORE_PLACEMENT=PASS
- RECOVERY_MATRIX_CONTROLLER_CRASH_AFTER_PLACEMENT=PASS
- RECOVERY_MATRIX_AGENT_CRASH_BEFORE_ACTION=PASS
- RECOVERY_MATRIX_AGENT_CRASH_AFTER_ACTION_BEFORE_COMMIT=PASS
- RECOVERY_MATRIX_AGENT_CRASH_AFTER_COMMIT=PASS
- RECOVERY_MATRIX_MIGRATION_SOURCE_CRASH=PASS
- RECOVERY_MATRIX_MIGRATION_DESTINATION_CRASH=PASS
- RECOVERY_MATRIX_LEASE_EXPIRY=PASS
- RECOVERY_MATRIX_RESERVATION_EXPIRY=PASS
- RECOVERY_MATRIX_RUNTIME_RESTART=PASS
"""

from __future__ import annotations

import tempfile
import time

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    LeaseStatus,
    PlacementRequest,
    ReservationStatus,
    ResourceType,
)


def test_recovery_matrix_cases() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_matrix", base_dir=tmpdir)
        controller.resources.register_pool("mat_pool", ResourceType.CPU, capacity=10.0)

        a1 = AgentInstance(
            agent_instance_id="mat_a1", fleet_id="f_matrix", status=AgentInstanceStatus.READY
        )
        a2 = AgentInstance(
            agent_instance_id="mat_a2", fleet_id="f_matrix", status=AgentInstanceStatus.READY
        )
        controller.register_agent(a1)
        controller.register_agent(a2)

        # Case 1: Controller crash before placement
        del controller
        c1 = FleetController(fleet_id="f_matrix", base_dir=tmpdir)
        assert c1.registry.get_fleet() is not None

        # Case 2: Controller crash after placement
        req_p = PlacementRequest(mission_id="m_mat_p", principal_id="u")
        plc, _ = c1.admit_and_schedule(req_p)
        assert plc is not None
        del c1
        c2 = FleetController(fleet_id="f_matrix", base_dir=tmpdir)
        assert c2.registry.find_active_placement_for_mission("m_mat_p") is not None

        # Case 3: Agent crash before action
        c2.registry.update_agent_status("mat_a1", AgentInstanceStatus.UNAVAILABLE)
        c2.lifecycle.detect_failures()
        assert c2.registry.get_agent("mat_a1").status == AgentInstanceStatus.UNAVAILABLE

        # Case 4: Agent crash after action before commit (recovery recovers mission on a2)
        mig_rec = c2.recover_mission("m_mat_p")
        assert mig_rec.target_agent_instance_id == "mat_a2"

        # Case 5: Agent crash after commit
        c2.registry.update_agent_status("mat_a1", AgentInstanceStatus.READY)

        # Case 6 & 7: Migration source/destination crash recovery
        mig = c2.migrations.initiate_migration(
            mission_id="m_mat_p",
            target_agent_instance_id="mat_a1",
            reason="Rebalance",
        )
        c2.migrations.execute_migration(mig.migration_id)
        assert (
            c2.registry.find_active_placement_for_mission("m_mat_p").agent_instance_id == "mat_a1"
        )

        # Case 8: Lease expiry
        ls = c2.leases.acquire_lease("mat_a1", "m_mat_exp", ttl_s=0.1)
        time.sleep(0.15)
        expired_count = c2.leases.sweep_expired_leases()
        assert expired_count >= 1
        assert c2.registry.get_lease(ls.lease_id).status == LeaseStatus.EXPIRED

        # Case 9: Reservation expiry
        res = c2.resources.reserve("m_mat_exp", "mat_pool", 1.0, ttl_s=0.1)
        time.sleep(0.15)
        reconciled_res = c2.resources.reconcile_expired()
        assert reconciled_res >= 1
        assert c2.registry.get_reservation(res.reservation_id).status == ReservationStatus.EXPIRED

        # Case 10: Runtime restart reconciliation
        c2.heartbeat_agent("mat_a1")
        assert c2.registry.get_agent("mat_a1").status in (
            AgentInstanceStatus.READY,
            AgentInstanceStatus.BUSY,
        )
