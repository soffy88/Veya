"""PQ §6: Fairness and Anti-Starvation Qualification.

Invariants:
- STARVATION=0: Low priority or burst-deferred missions are not permanently starved.
- FAIRNESS=PASS: Queue aging ensures progressive elevation of waiting missions.
"""

from __future__ import annotations

import tempfile
import time

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
)


def test_fairness_queue_aging_anti_starvation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_fairness_pqa",
            base_dir=tmpdir,
            global_max_missions=1,
            per_principal_max_missions=1,
            aging_interval_s=10.0,
        )

        agent = AgentInstance(
            agent_instance_id="agent_single",
            fleet_id="f_fairness_pqa",
            capability_profile=["task"],
            status=AgentInstanceStatus.READY,
        )
        controller.register_agent(agent)

        # Principal VIP submits a high-priority mission (priority 100) -> immediately placed
        req_vip = PlacementRequest(
            mission_id="m_vip_active",
            principal_id="principal_vip",
            required_capabilities=["task"],
            priority=100,
        )
        plc_vip, _ = controller.admit_and_schedule(req_vip)
        assert plc_vip is not None

        # Principal Standard submits low-priority mission (priority 1) -> queued
        req_std = PlacementRequest(
            mission_id="m_std_waiting",
            principal_id="principal_std",
            required_capabilities=["task"],
            priority=1,
        )
        controller.admit_and_schedule(req_std)

        # Principal VIP submits another high-priority mission (priority 50) -> queued
        req_vip_next = PlacementRequest(
            mission_id="m_vip_next",
            principal_id="principal_vip",
            required_capabilities=["task"],
            priority=50,
        )
        controller.admit_and_schedule(req_vip_next)

        # Initial queue check: vip_next has priority 50, std has priority 1
        queue = controller.registry.list_queue()
        assert len(queue) == 2

        # Simulate progressive aging on Standard mission (it has been waiting for 200s)
        std_entry = next(q for q in queue if q.mission_id == "m_std_waiting")
        std_entry.enqueued_at = time.time() - 200.0
        controller.registry.enqueue(std_entry)

        # Calculate effective priorities
        now = time.time()
        eff_std = controller.scheduler.calculate_effective_priority(std_entry, now)
        vip_entry = next(q for q in queue if q.mission_id == "m_vip_next")
        eff_vip = controller.scheduler.calculate_effective_priority(vip_entry, now)

        # Effective priority of aged standard mission must exceed new VIP request
        assert eff_std > eff_vip, f"Standard effective priority {eff_std} must exceed VIP {eff_vip}"

        # Release active VIP mission and process queue
        controller.release_placement("m_vip_active")
        next_placed = controller.process_queue()

        # The aged standard mission MUST be scheduled next (STARVATION=0)
        assert len(next_placed) == 1
        assert next_placed[0].mission_id == "m_std_waiting"
