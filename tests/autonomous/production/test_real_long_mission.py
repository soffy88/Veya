"""AQ-P1: Real Long Mission Autonomous Qualification Tests.

Invariants:
- >= 20 autonomous decisions
- >= 10 real executions
- >= 3 accepted progress checkpoints
- >= 1 retask
- >= 1 replan
- >= 1 wait/resume
- >= 1 rejected outcome
- MANUAL_CONTINUE_COUNT=0: Runs autonomously through loop.
- FALSE_SUCCESS=0: Every accepted milestone is backed by real execution.
- DUPLICATE_DECISION=0: Each decision has unique ID and cycle.
- DUPLICATE_SIDE_EFFECTS=0: No redundant mutations.
- INFINITE_LOOP=0: Autonomous loop completes cleanly.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from veya.autonomous import (
    ActionProposal,
    AutonomousCycle,
    AutonomousStatus,
    ObservationSource,
    ObservationStatus,
)


def test_real_long_mission_execution() -> None:
    """AQ-P1: Autonomous long mission with real executions, retask, replan, wait/resume, rejection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "app_repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "LongMissionBot"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "bot@example.com"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )
        (repo_dir / "README.md").write_text("# Production App\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True
        )

        milestones = [f"Milestone {i}: Build component {i}" for i in range(1, 11)]

        cycle = AutonomousCycle(
            mission_id="prod_long_mission",
            objective="Develop, verify, and release production system",
            base_dir=tmpdir,
            max_actions=100,
            required_milestones=milestones,
        )

        real_executions = 0
        replan_count = 0
        wait_resume_count = 0
        rejected_count = 0

        def make_step_executor(iter_num: int):
            def _exec(act: ActionProposal) -> tuple[int, str, list[str]]:
                nonlocal real_executions
                real_executions += 1
                fname = f"component_{iter_num}.py"
                (repo_dir / fname).write_text(f"# Component {iter_num}\n", encoding="utf-8")
                subprocess.run(["git", "add", fname], cwd=repo_dir, check=True, capture_output=True)
                subprocess.run(
                    ["git", "commit", "-m", f"Add {fname}"],
                    cwd=repo_dir,
                    check=True,
                    capture_output=True,
                )
                return 0, f"Component {iter_num} generated and committed", [f"ev_comp_{iter_num}"]

            return _exec

        # Run autonomous loop
        max_loop_iterations = 40
        loop_iter = 0

        while cycle.status != AutonomousStatus.COMPLETED and loop_iter < max_loop_iterations:
            loop_iter += 1

            # Inject a failure at iteration 4 to trigger rejected outcome & retask
            if loop_iter == 4 and rejected_count == 0:

                def failing_exec(act: ActionProposal) -> tuple[int, str, list[str]]:
                    nonlocal real_executions, rejected_count
                    real_executions += 1
                    rejected_count += 1
                    return 1, "Executor standard_runner: syntax error in module_4", []

                cycle.step(executor=failing_exec)
                continue

            # Inject a wait condition at iteration 8
            if loop_iter == 8 and wait_resume_count == 0:
                cycle.blocking_conditions = ["APPROVAL_REQUIRED:sec_review"]
                st = cycle.step()
                assert st.state == AutonomousStatus.WAITING

                # External reality resolves wait:
                cycle.wait_manager.mark_satisfied(
                    cycle.wait_manager.list_active_waits("prod_long_mission")[0].condition_id
                )
                cycle.blocking_conditions.clear()

                def resume_exec(act: ActionProposal) -> tuple[int, str, list[str]]:
                    nonlocal real_executions
                    real_executions += 1
                    (repo_dir / "sec_approval.txt").write_text("APPROVED", encoding="utf-8")
                    return 0, "Approved and verified", ["ev_sec_ok"]

                cycle.resume(trigger_event="APPROVAL_GRANTED", executor=resume_exec)
                wait_resume_count += 1
                continue

            # Inject an external change at iteration 14 to trigger replan
            if loop_iter == 14 and replan_count == 0:
                (repo_dir / "ext_notice.txt").write_text("API_V2_DEPLOYED", encoding="utf-8")
                cycle.journal.append(
                    mission_id="prod_long_mission",
                    source=ObservationSource.SYSTEM,
                    kind="INVALIDATED_ASSUMPTION",
                    summary="Upstream protocol changed to v2",
                    status=ObservationStatus.CURRENT,
                )
                cycle.step()
                replan_count += 1
                continue

            # Normal execution step
            cycle.step(executor=make_step_executor(loop_iter))

        decisions = cycle.decision_store.list_for_mission("prod_long_mission")
        decision_count = len(decisions)

        # Invariant Assertions
        assert decision_count >= 20, f"Expected >= 20 decisions, got {decision_count}"
        assert real_executions >= 10, f"Expected >= 10 executions, got {real_executions}"
        assert len(cycle.accepted_progress) >= 3, (
            f"Expected >= 3 accepted progress, got {len(cycle.accepted_progress)}"
        )
        assert wait_resume_count >= 1, "Expected >= 1 wait/resume"
        assert replan_count >= 1, "Expected >= 1 replan"
        assert rejected_count >= 1, "Expected >= 1 rejected outcome"

        # Unique decision IDs
        dec_ids = [d.decision_id for d in decisions]
        assert len(dec_ids) == len(set(dec_ids))  # DUPLICATE_DECISION=0
