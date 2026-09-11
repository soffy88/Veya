from __future__ import annotations

import pytest

from runtime.execution.long_running import DuplicateFailedAction
from server.goal_run.harness_adapter import GoalRunHarnessAdapter
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.store import load_goal_run, save_goal_run


@pytest.mark.asyncio
async def test_canonical_goalrun_harness_failover_checkpoint_restart_and_pass(tmp_path):
    state = GoalRunState(
        goal_id="goal-integration",
        goal_text="long running integration",
        status=GoalStatus.running,
        budget={
            "max_wall_s": 3600,
            "max_tool_calls": 20,
            "max_replans": 5,
            "computer_id": "computer-stable",
        },
        tasks={
            "task-1": TaskNode(
                id="task-1",
                title="task",
                instruction="do task",
                acceptance=["pass"],
                depends_on=[],
                assignee="hicode",
                status=TaskStatus.running,
            )
        },
    )
    save_goal_run(state, str(tmp_path))

    adapter = GoalRunHarnessAdapter.attach(state, str(tmp_path))
    assert adapter.goal_run_id == state.goal_id
    assert adapter.computer_id == "computer-stable"

    adapter.harness.before_action("tool", {"path": "result"}, {"computer": adapter.computer_id})
    adapter.harness.record_action_failure(
        "tool", {"path": "result"}, "temporary failure", {"computer": adapter.computer_id}
    )
    with pytest.raises(DuplicateFailedAction):
        adapter.harness.before_action("tool", {"path": "result"}, {"computer": adapter.computer_id})

    calls: list[str] = []

    async def provider(name: str):
        calls.append(name)
        if name == "primary":
            raise TimeoutError("provider timeout")
        return {"continued": True}

    assert (await adapter.provider_call(provider, ["primary", "fallback"]))["continued"]
    adapter.observe(task_id="task-1")
    adapter.persist(reason="integration_checkpoint")
    save_goal_run(state, str(tmp_path))

    restarted_state = load_goal_run(str(tmp_path), state.goal_id)
    assert restarted_state is not None
    restarted = GoalRunHarnessAdapter.attach(restarted_state, str(tmp_path))
    assert restarted.goal_run_id == state.goal_id
    assert restarted.computer_id == adapter.computer_id
    assert calls == ["primary", "fallback"]

    assert restarted.apply_verification("PASS", evidence={"verification_os": "PASS"}) == "completed"
    restarted_state.status = GoalStatus.completed
    save_goal_run(restarted_state, str(tmp_path))
    completed = load_goal_run(str(tmp_path), state.goal_id)
    assert completed is not None
    assert completed.goal_id == state.goal_id
    assert completed.status == GoalStatus.completed
    assert completed.runtime_checkpoint["long_running"]["status"] == "completed"
