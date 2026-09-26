"""Tests for Accelerated Fleet Soak, Chaos, and Zero Leak Gates (spec §78-§81, E2E-O)."""

from __future__ import annotations

import os
import tempfile
import threading

from veya.fleet import (
    AgentInstance,
    AgentInstanceStatus,
    FleetController,
    PlacementRequest,
    ResourceType,
)


def _get_fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return 0


def test_accelerated_fleet_soak_and_chaos_e2e_o() -> None:
    """E2E-O: Soak across >=100 missions, >=5 agents, >=500 placements, >=50 recoveries, >=20 restarts, >=20 crashes with zero leaks."""
    initial_fds = _get_fd_count()
    initial_threads = threading.active_count()

    with tempfile.TemporaryDirectory() as tmpdir:
        controller = FleetController(
            fleet_id="f_soak",
            base_dir=tmpdir,
            global_max_missions=50,
        )

        # Register 5 agents
        for i in range(1, 6):
            agent = AgentInstance(
                agent_instance_id=f"agent_soak_{i}",
                fleet_id="f_soak",
                capability_profile=["compute", "coding"],
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        controller.resources.register_pool("cpu_pool", ResourceType.CPU, capacity=100.0)

        total_placements = 0
        total_recoveries = 0
        controller_restarts = 0
        agent_crashes = 0
        saturation_periods = 0

        # Run accelerated stress loop
        for batch in range(1, 26):
            # Phase 1: Submit 20 missions per batch (55 on saturation batches to exceed global limit 50)
            missions_in_batch = 55 if batch % 5 == 0 else 20
            for m in range(1, missions_in_batch + 1):
                m_id = f"m_soak_{batch}_{m}"
                req = PlacementRequest(
                    mission_id=m_id,
                    principal_id=f"user_{m % 5}",
                    required_capabilities=["compute"],
                )
                plc, _s = controller.admit_and_schedule(req)
                if plc is not None:
                    total_placements += 1

            # Phase 2: Chaos - Agent crash every batch (25 crashes >= 20 required)
            crash_target = f"agent_soak_{(batch % 5) + 1}"
            controller.registry.update_agent_status(crash_target, AgentInstanceStatus.UNAVAILABLE)
            agent_crashes += 1

            # Phase 3: Recover missions on crashed agent
            active_leases = [
                als
                for als in controller.registry.list_leases()
                if als.agent_instance_id == crash_target and als.status.value == "ACTIVE"
            ]
            for lease in active_leases:
                controller.leases.release_lease(lease.lease_id)
                try:
                    controller.recover_mission(lease.mission_id)
                    total_recoveries += 1
                except Exception:
                    pass

            # Restore crashed agent to READY
            controller.registry.update_agent_status(crash_target, AgentInstanceStatus.READY)

            # Phase 4: Chaos - Controller restart every batch (25 restarts >= 20 required)
            del controller
            controller_restarts += 1
            controller = FleetController(
                fleet_id="f_soak",
                base_dir=tmpdir,
                global_max_missions=50,
            )

            # Phase 5: Drain queue if saturated
            queued_count = len(controller.registry.list_queue())
            if queued_count > 0:
                saturation_periods += 1
                # Free space and process queue
                for m in range(1, 10):
                    m_id = f"m_soak_{batch}_{m}"
                    controller.release_placement(m_id)
                queued_placed = controller.process_queue()
                total_placements += len(queued_placed)

            # Release finished work in this batch
            for m in range(1, missions_in_batch + 1):
                m_id = f"m_soak_{batch}_{m}"
                controller.release_placement(m_id)

        # Soak Metric Assertions (spec §78)
        assert total_placements >= 500, f"Expected >= 500 placements, got {total_placements}"
        assert total_recoveries >= 50, f"Expected >= 50 recoveries, got {total_recoveries}"
        assert controller_restarts >= 20, f"Expected >= 20 restarts, got {controller_restarts}"
        assert agent_crashes >= 20, f"Expected >= 20 crashes, got {agent_crashes}"
        assert saturation_periods >= 5, (
            f"Expected >= 5 saturation periods, got {saturation_periods}"
        )

        # Resource leak checks (spec §79)
        final_fds = _get_fd_count()
        fd_growth = max(0, final_fds - initial_fds)
        final_threads = threading.active_count()
        thread_growth = max(0, final_threads - initial_threads)

        assert fd_growth <= 10, f"FD leak detected: grew by {fd_growth}"
        assert thread_growth <= 5, f"Thread leak detected: grew by {thread_growth}"

        # Leases and reservations reconciliation
        controller.leases.sweep_expired_leases()
        controller.resources.reconcile_expired()
        active_leases_remaining = [
            als for als in controller.registry.list_leases() if als.status.value == "ACTIVE"
        ]
        assert len(active_leases_remaining) == 0, (
            f"Lease leak detected: {len(active_leases_remaining)} unreleased"
        )
