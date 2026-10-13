"""P3: ExecutionControl drives a real ExecutorAdapter execution.

Proves the P3 lifecycle: GoalRun -> ExecutorAdapter.start() -> terminal status,
with activity events recorded and no second durable authority.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from server.goal_run.execution_control import (
    ExecutionControl,
    ExecutionControlPhase,
    get_execution_control,
)
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.store import save_goal_run
from veya.remote.executor_adapters import register_default_adapters


def _make_goal_run(root: Path, executor: str = "local2") -> str:
    goal_id = f"goal_{uuid.uuid4().hex[:8]}"
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    state = GoalRunState(
        goal_id=goal_id,
        goal_text="P3 execution control test",
        status=GoalStatus.pending_execution,
        requested_executor=executor,
    )
    state.tasks[task_id] = TaskNode(
        id=task_id,
        title="run echo",
        instruction="echo p3-ok",
        acceptance=["command succeeds"],
        depends_on=[],
        assignee=executor,
        status=TaskStatus.pending,
    )
    save_goal_run(state, str(root))
    return goal_id


@pytest.mark.asyncio
async def test_run_task_drives_local2_to_completed(tmp_path: Path):
    register_default_adapters()
    goal_id = _make_goal_run(tmp_path)
    control = get_execution_control(str(tmp_path))

    result = await control.run_task(goal_id, command="echo p3-ok", executor_id="local2")
    assert result.goal_status == GoalStatus.completed
    assert result.phase == ExecutionControlPhase.COMPLETED

    status = await control.status(goal_id)
    assert status.goal_status == GoalStatus.completed


@pytest.mark.asyncio
async def test_run_task_records_activity_stream(tmp_path: Path):
    register_default_adapters()
    goal_id = _make_goal_run(tmp_path)
    control = ExecutionControl(str(tmp_path))
    await control.run_task(goal_id, command="echo p3-stream", executor_id="local2")

    events, cursor = await control.stream(goal_id, cursor=0)
    types = [e.event_type for e in events]
    assert "EXECUTOR_SELECTED" in types
    assert "PROCESS_STARTED" in types
    assert "GOALRUN_COMPLETED" in types
    # P7: adapter stdout reaches the unified activity stream.
    assert "OUTPUT" in types
    assert cursor >= len(events)
    # sequences are monotonic
    seqs = [e.sequence for e in events]
    assert seqs == sorted(seqs)


@pytest.mark.asyncio
async def test_run_task_failure_marks_goal_failed(tmp_path: Path):
    register_default_adapters()
    goal_id = _make_goal_run(tmp_path)
    control = ExecutionControl(str(tmp_path))
    # A command that exits non-zero must fail the GoalRun, never fake success.
    result = await control.run_task(goal_id, command="false", executor_id="local2")
    assert result.goal_status == GoalStatus.failed


@pytest.mark.asyncio
async def test_run_task_unknown_executor_refused(tmp_path: Path):
    goal_id = _make_goal_run(tmp_path)
    control = ExecutionControl(str(tmp_path))
    with pytest.raises(RuntimeError, match="no ExecutorAdapter registered"):
        await control.run_task(goal_id, command="echo x", executor_id="nope")
