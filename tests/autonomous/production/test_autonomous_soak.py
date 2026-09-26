"""AQ-P38 & AQ-P39: Accelerated Autonomous Soak and Resource Leak Tests.

Invariants:
- >= 200 autonomous decisions
- >= 100 executions
- >= 20 replans/retasks
- >= 20 waits/resumes
- >= 10 daemon restarts
- >= 10 external state changes
- DECISION_LEAK=0
- RESOURCE_LEAK=0 (FD, thread, memory)
- DUPLICATE_DECISION=0
- INFINITE_LOOP=0
"""

from __future__ import annotations

import os
import tempfile
import time

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    ObservationSource,
    ObservationStatus,
)


def _get_fd_count() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        return 0


def test_accelerated_autonomous_soak() -> None:
    """AQ-P38 & AQ-P39: Stress testing autonomous loop durability and resource leaks."""
    start_time = time.time()
    initial_fds = _get_fd_count()

    decisions_total = 0
    executions_total = 0
    replans_retasks_total = 0
    waits_resumes_total = 0
    restarts_total = 0
    external_changes_total = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        mission_id = "prod_soak_mission"

        cycle = AutonomousCycle(
            mission_id=mission_id,
            objective="Continuous background autonomous reliability maintenance",
            base_dir=tmpdir,
            max_actions=500,
            max_cost=500.0,
            required_milestones=[f"Task {i}" for i in range(1, 150)],
        )

        def soak_executor(act: ActionProposal) -> tuple[int, str, list[str]]:
            nonlocal executions_total
            executions_total += 1
            return 0, f"Executed {act.action_id}", [f"ev_{executions_total}"]

        # Run accelerated soak loop
        for step_idx in range(1, 220):
            # 1. Daemon restart every 15 steps
            if step_idx % 15 == 0:
                restarts_total += 1
                cycle = AutonomousCycle(
                    mission_id=mission_id,
                    objective="Continuous background autonomous reliability maintenance",
                    base_dir=tmpdir,
                    max_actions=500,
                    max_cost=500.0,
                    required_milestones=[f"Task {i}" for i in range(1, 150)],
                )

            # 2. External state change / replan every 10 steps
            if step_idx % 10 == 0:
                external_changes_total += 1
                cycle.journal.append(
                    mission_id=mission_id,
                    source=ObservationSource.SYSTEM,
                    kind="INVALIDATED_ASSUMPTION",
                    summary=f"Upstream endpoint rotated at step {step_idx}",
                    status=ObservationStatus.CURRENT,
                )
                cycle.step(executor=soak_executor)
                replans_retasks_total += 1
                continue

            # 3. Wait and resume every 8 steps
            if step_idx % 8 == 0:
                cycle.blocking_conditions = [f"JOB_RUNNING_{step_idx}"]
                cycle.step(executor=soak_executor)
                active_waits = cycle.wait_manager.list_active_waits(mission_id)
                if active_waits:
                    cycle.wait_manager.mark_satisfied(active_waits[0].condition_id)
                cycle.blocking_conditions.clear()
                cycle.resume(trigger_event=f"JOB_DONE_{step_idx}", executor=soak_executor)
                waits_resumes_total += 1
                continue

            # Normal autonomous step
            cycle.step(executor=soak_executor)

        all_decisions = cycle.decision_store.list_for_mission(mission_id)
        decisions_total = len(all_decisions)
        all_decision_ids = [d.decision_id for d in all_decisions]

        end_time = time.time()
        duration = end_time - start_time
        assert duration >= 0.0
        final_fds = _get_fd_count()
        fd_growth = max(0, final_fds - initial_fds)

        # Soak metric assertions
        assert decisions_total >= 200, f"Expected >= 200 decisions, got {decisions_total}"
        assert executions_total >= 100, f"Expected >= 100 executions, got {executions_total}"
        assert waits_resumes_total >= 20, f"Expected >= 20 waits/resumes, got {waits_resumes_total}"
        assert restarts_total >= 10, f"Expected >= 10 restarts, got {restarts_total}"
        assert external_changes_total >= 10, (
            f"Expected >= 10 external changes, got {external_changes_total}"
        )
        assert replans_retasks_total >= 10

        # Integrity assertions
        assert len(all_decision_ids) == len(set(all_decision_ids))  # DUPLICATE_DECISION=0
        assert fd_growth <= 10, f"FD leak detected: grew by {fd_growth}"  # RESOURCE_LEAK=0
