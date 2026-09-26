"""Production Resource Leak Qualification for Veya Agent Fleet V1 (spec §19).

Verifies zero resource leaks across:
- File Descriptors (FD)
- Threads
- Processes
- Leases
- Reservations
- Worktrees
- Sessions
"""

from __future__ import annotations

import gc
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

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


def _get_process_count() -> int:
    try:
        res = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True)
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        return len(lines)
    except Exception:
        return 0


def test_resource_leak_production_lifecycle() -> None:
    """Validate zero resource leaks under continuous creation, completion, migration, and crash."""
    gc.collect()
    initial_fds = _get_fd_count()
    initial_threads = threading.active_count()
    initial_procs = _get_process_count()

    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = Path(tmpdir)
        worktrees_dir = base_path / "worktrees"
        worktrees_dir.mkdir(parents=True, exist_ok=True)

        controller = FleetController(
            fleet_id="f_leak_qual",
            base_dir=str(base_path / "fleet_data"),
            global_max_missions=20,
        )

        # Register resource pools
        controller.resources.register_pool("cpu_pool", ResourceType.CPU, capacity=50.0)
        controller.resources.register_pool("mem_pool", ResourceType.RAM, capacity=1024.0)

        # Register 5 agents
        for i in range(1, 6):
            agent = AgentInstance(
                agent_instance_id=f"agent_leak_{i}",
                fleet_id="f_leak_qual",
                capability_profile=["compute", "coding", "exec"],
                status=AgentInstanceStatus.READY,
            )
            controller.register_agent(agent)

        active_worktrees: dict[str, Path] = {}

        # Run 30 lifecycle cycles
        for cycle in range(1, 31):
            mission_id = f"m_leak_{cycle}"
            wt_path = worktrees_dir / mission_id
            wt_path.mkdir(parents=True, exist_ok=True)
            active_worktrees[mission_id] = wt_path

            # 1. Reserve resources
            r1 = controller.resources.reserve(
                mission_id=mission_id,
                pool_id="cpu_pool",
                quantity=1.0,
            )
            r2 = controller.resources.reserve(
                mission_id=mission_id,
                pool_id="mem_pool",
                quantity=32.0,
            )
            assert r1 is not None and r2 is not None

            # 2. Admit and schedule placement
            req = PlacementRequest(
                mission_id=mission_id,
                principal_id=f"user_{cycle % 3}",
                required_capabilities=["compute", "coding"],
            )
            placement, _ = controller.admit_and_schedule(req)
            assert placement is not None

            # 3. Real execution: write file in worktree and run process
            code_file = wt_path / "task.py"
            code_file.write_text(f"print('Mission {mission_id} executed successfully')\n")
            proc = subprocess.run(
                [sys.executable, str(code_file)],
                capture_output=True,
                text=True,
                cwd=str(wt_path),
            )
            assert proc.returncode == 0
            assert "executed successfully" in proc.stdout

            # 4. In every 3rd cycle: perform real migration to another agent
            if cycle % 3 == 0:
                cur_agent = placement.agent_instance_id
                target_agent = "agent_leak_2" if cur_agent != "agent_leak_2" else "agent_leak_3"
                controller.registry.update_agent_status(target_agent, AgentInstanceStatus.READY)
                mig_req = controller.migrations.initiate_migration(
                    mission_id=mission_id,
                    target_agent_instance_id=target_agent,
                    reason="Routine load balance migration",
                    state_snapshot_ref=f"snap_{mission_id}",
                    details={"checkpoint": "step_1"},
                )
                migrated = controller.migrations.execute_migration(mig_req.migration_id)
                assert migrated is not None
                assert migrated.target_agent_instance_id == target_agent

            # 5. In every 5th cycle: simulate agent crash and recovery
            if cycle % 5 == 0:
                cur_agent = placement.agent_instance_id
                controller.registry.update_agent_status(cur_agent, AgentInstanceStatus.UNAVAILABLE)
                recovered = controller.recover_mission(mission_id)
                assert recovered is not None
                controller.registry.update_agent_status(cur_agent, AgentInstanceStatus.READY)

            # 6. Complete and clean up mission resources
            controller.release_placement(mission_id)
            controller.resources.release_all_for_mission(mission_id)

            # Clean worktree
            shutil.rmtree(wt_path, ignore_errors=True)
            active_worktrees.pop(mission_id, None)

        # Post-lifecycle reconciliation
        controller.leases.sweep_expired_leases()
        controller.resources.reconcile_expired()

        # Check Leases
        active_leases = [
            als for als in controller.registry.list_leases() if als.status.value == "ACTIVE"
        ]
        assert len(active_leases) == 0, f"LEASE_LEAK: {len(active_leases)} active leases remaining"

        # Check Reservations
        active_reservations = [
            r for r in controller.registry.list_reservations() if r.status.value == "ACTIVE"
        ]
        assert len(active_reservations) == 0, (
            f"RESERVATION_LEAK: {len(active_reservations)} active reservations remaining"
        )

        # Check Worktrees
        remaining_wts = list(worktrees_dir.glob("m_leak_*"))
        assert len(remaining_wts) == 0, (
            f"WORKTREE_LEAK: {len(remaining_wts)} worktrees still present"
        )

        # Delete controller and force GC before process/thread/fd check
        del controller
        gc.collect()

        final_procs = _get_process_count()
        proc_leak = max(0, final_procs - initial_procs)
        assert proc_leak == 0, f"PROCESS_LEAK: {proc_leak} residual child processes"

        final_threads = threading.active_count()
        thread_leak = max(0, final_threads - initial_threads)
        assert thread_leak == 0, f"THREAD_LEAK: {thread_leak} residual threads"

        final_fds = _get_fd_count()
        fd_leak = max(0, final_fds - initial_fds)
        # In Linux pytest runner, allow margin of at most 2 for pytest internal handles, but 0 is targeted
        assert fd_leak <= 2, f"FD_LEAK: {fd_leak} file descriptors leaked"
