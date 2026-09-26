"""PQ §16: Reservation Failure Scenarios and Zero Leak Gates.

Invariants:
- RESERVATION_LEAK=0
- RESOURCE_OVERCOMMIT=0
"""

from __future__ import annotations

import tempfile
import time

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
    ReservationStatus,
    ResourceType,
)


def test_reservation_failure_and_leak_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_res_failures", base_dir=tmpdir)
        controller.resources.register_pool("gpu_res_pool", ResourceType.GPU, capacity=2.0)

        # Scenario 1: Reservation succeeds, then agent crashes -> release restores capacity
        agent = AgentInstance(
            agent_instance_id="agent_gpu_crash",
            fleet_id="f_res_failures",
            capability_profile=["gpu"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        res1 = controller.resources.reserve(
            mission_id="m_res_crash",
            pool_id="gpu_res_pool",
            quantity=1.0,
            agent_instance_id="agent_gpu_crash",
        )
        assert res1.status == ReservationStatus.RESERVED

        # Agent crashes and mission is cancelled/released
        controller.resources.release_all_for_mission("m_res_crash")
        pool = controller.registry.get_pool("gpu_res_pool")
        assert pool.available == 2.0  # Capacity fully restored

        # Scenario 2: Reservation succeeds, but placement fails (e.g. invalid capability)
        controller.resources.reserve(
            mission_id="m_res_fail_plc",
            pool_id="gpu_res_pool",
            quantity=1.0,
        )
        # Placement fails
        req_bad = PlacementRequest(
            mission_id="m_res_fail_plc",
            principal_id="u",
            required_capabilities=["unsupported_hardware"],
        )
        plc, _ = controller.admit_and_schedule(req_bad)
        assert plc is None

        # Clean up failed mission
        controller.resources.release_all_for_mission("m_res_fail_plc")
        pool = controller.registry.get_pool("gpu_res_pool")
        assert pool.available == 2.0

        # Scenario 3: Reservation expires naturally
        controller.resources.reserve(
            mission_id="m_res_expire",
            pool_id="gpu_res_pool",
            quantity=2.0,
            ttl_s=0.1,
        )
        time.sleep(0.15)
        expired_count = controller.resources.reconcile_expired()
        assert expired_count >= 1
        pool = controller.registry.get_pool("gpu_res_pool")
        assert pool.available == 2.0  # RESERVATION_LEAK=0
