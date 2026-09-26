"""Tests for Resource Pools, Anti-Starvation Fairness, and Reservation Leaks (AF-P2, E2E-C, E2E-M, E2E-N)."""

from __future__ import annotations

import tempfile
import time

import pytest

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
    ReservationStatus,
    ResourceExhaustedError,
    ResourceType,
)


def test_fairness_anti_starvation_e2e_c() -> None:
    """E2E-C: Principal B with small workload is not starved by Principal A's burst (STARVATION=0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_fair",
            base_dir=tmpdir,
            global_max_missions=1,
            per_principal_max_missions=1,  # Strict concurrency of 1 per principal
        )
        agent = AgentInstance(
            agent_instance_id="agent_1",
            fleet_id="f_fair",
            capability_profile=["worker"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # Principal A submits 10 missions
        for i in range(1, 11):
            req = PlacementRequest(
                mission_id=f"a_{i}",
                principal_id="principal_A",
                required_capabilities=["worker"],
                priority=10,
            )
            controller.admit_and_schedule(req)

        # Principal B submits 1 mission with lower priority
        req_b = PlacementRequest(
            mission_id="b_1",
            principal_id="principal_B",
            required_capabilities=["worker"],
            priority=5,
        )
        controller.admit_and_schedule(req_b)

        # Check queue
        queue = controller.registry.list_queue()
        assert len(queue) >= 9

        # Simulate time aging on Principal B's entry to test anti-starvation
        b_entry = next(q for q in queue if q.mission_id == "b_1")
        # Artificially age b_entry by 300 seconds
        b_entry.enqueued_at = time.time() - 300.0
        controller.registry.enqueue(b_entry)

        effective_b = controller.scheduler.calculate_effective_priority(b_entry)
        assert effective_b > 10.0, (
            "Aging must boost effective priority above Principal A's baseline"
        )


def test_resource_reservation_e2e_m_and_leak_e2e_n() -> None:
    """E2E-M & E2E-N: Resource reservations prevent overcommit and release on mission completion/reconciliation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(fleet_id="f_res", base_dir=tmpdir)
        # Register a single GPU slot pool
        controller.resources.register_pool(
            pool_id="gpu_pool_1",
            resource_type=ResourceType.GPU,
            capacity=1.0,
        )

        # Mission 1 reserves the 1 GPU slot
        res1 = controller.resources.reserve(
            mission_id="m_gpu_1",
            pool_id="gpu_pool_1",
            quantity=1.0,
        )
        assert res1.status == ReservationStatus.RESERVED
        pool = controller.registry.get_pool("gpu_pool_1")
        assert pool.available == 0.0

        # Mission 2 attempting to reserve GPU slot must fail / raise ResourceExhaustedError (RESOURCE_OVERCOMMIT=0)
        with pytest.raises(ResourceExhaustedError):
            controller.resources.reserve(
                mission_id="m_gpu_2",
                pool_id="gpu_pool_1",
                quantity=1.0,
            )

        # Activate reservation 1
        controller.resources.activate_reservation(res1.reservation_id)
        pool = controller.registry.get_pool("gpu_pool_1")
        assert pool.allocated == 1.0

        # Mission 1 concludes -> release all reservations (RESOURCE_RESERVATION_LEAK=0)
        released = controller.resources.release_all_for_mission("m_gpu_1")
        assert released == 1
        pool = controller.registry.get_pool("gpu_pool_1")
        assert pool.available == 1.0
        assert pool.allocated == 0.0
        assert pool.reserved == 0.0

        # Now Mission 2 can reserve successfully
        res2 = controller.resources.reserve(
            mission_id="m_gpu_2",
            pool_id="gpu_pool_1",
            quantity=1.0,
        )
        assert res2.status == ReservationStatus.RESERVED
