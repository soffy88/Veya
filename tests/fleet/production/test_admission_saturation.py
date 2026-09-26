"""PQ §5: Admission, Saturation, and Backpressure under Stress.

Invariants:
- OVERCOMMIT=0
- UNBOUNDED_QUEUE=0
- BACKPRESSURE=PASS
- ADMISSION_REJECTION_OR_QUEUE=CORRECT
"""

from __future__ import annotations

import tempfile

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
    ResourceType,
)


def test_admission_saturation_and_backpressure() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        # Constrain global max missions to 5
        controller = FleetController(
            fleet_id="f_saturation_pqa",
            base_dir=tmpdir,
            global_max_missions=5,
            per_principal_max_missions=3,
        )

        controller.resources.register_pool("core_pool", ResourceType.CPU, capacity=10.0)

        agent = AgentInstance(
            agent_instance_id="agent_sat_1",
            fleet_id="f_saturation_pqa",
            capability_profile=["core"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # 1. Fill capacity up to global limit
        placed = []
        for i in range(1, 6):
            req = PlacementRequest(
                mission_id=f"m_sat_run_{i}",
                principal_id=f"user_{i % 2}",  # Split across 2 users so per_principal limit not exceeded
                required_capabilities=["core"],
            )
            plc, _ = controller.admit_and_schedule(req)
            assert plc is not None
            placed.append(plc)

        assert len(placed) == 5

        # 2. Check backpressure saturation metric
        health = controller.get_health()
        assert health.active_missions == 5

        # 3. Next admissions must be queued, NOT rejected with error or overcommitted
        deferred_ids = []
        for j in range(1, 6):
            req_def = PlacementRequest(
                mission_id=f"m_sat_def_{j}",
                principal_id="user_queued",
                required_capabilities=["core"],
            )
            plc_def, status_def = controller.admit_and_schedule(req_def)
            assert plc_def is None
            assert "DEFERRED" in status_def
            deferred_ids.append(f"m_sat_def_{j}")

        # Check queue
        queue = controller.registry.list_queue()
        assert len(queue) == 5
        for q_id in deferred_ids:
            assert any(q.mission_id == q_id for q in queue)

        # 4. Release two active missions -> process queue -> queued missions admitted
        controller.release_placement("m_sat_run_1")
        controller.release_placement("m_sat_run_2")

        newly_placed = controller.process_queue()
        assert len(newly_placed) == 2, (
            f"Expected 2 queue entries processed, got {len(newly_placed)}"
        )

        # Verify active placements count remains <= 5 (OVERCOMMIT=0)
        active_now = [p for p in controller.registry.list_placements() if p.status == "ACTIVE"]
        assert len(active_now) <= 5, "Total active placements must never exceed global capacity"
