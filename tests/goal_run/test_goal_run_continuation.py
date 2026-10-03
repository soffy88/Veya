"""P5 GoalRun continuation: same goal_run_id, new execution attempts.

Acceptance: continue() never forks a new GoalRun; each continuation appends
an attempt; history (tasks, block reasons, events) is preserved; a completed
goal cannot be continued and therefore cannot duplicate execution.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.goal_run.models import GoalStatus, TaskStatus
from server.goal_run.pre_admission import (
    bind_continuation_execution,
    continue_goal_run,
    create_pending_run,
    fail_pre_admission,
    load_goal_run,
    reconcile_task,
)
from server.goal_run.store import read_events


def _failed_goal(tmp_path: Path) -> tuple[str, str]:
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="p5-dispatch",
        session_id="p5-session",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "do it"}],
    )
    task_id = next(iter(pre.state.tasks))
    fail_pre_admission(project_root=str(tmp_path), goal_run_id=pre.state.goal_id, reason="boom")
    return pre.state.goal_id, task_id


def test_continue_appends_attempt_on_same_goal(tmp_path: Path) -> None:
    goal_id, _ = _failed_goal(tmp_path)
    first = continue_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="retry")
    assert first["attempt"] == 1
    assert first["execution_id"] is None
    assert first["status"] == "pending"

    state = load_goal_run(str(tmp_path), goal_id)
    assert state is not None
    assert state.goal_id == goal_id
    assert len(state.execution_attempts) == 1

    second = continue_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="retry2")
    assert second["attempt"] == 2
    assert load_goal_run(str(tmp_path), goal_id).execution_attempts[1]["attempt"] == 2


def test_continue_rearms_blocked_tasks_and_keeps_history(tmp_path: Path) -> None:
    goal_id, task_id = _failed_goal(tmp_path)
    before = load_goal_run(str(tmp_path), goal_id)
    assert before is not None
    assert before.tasks[task_id].status == TaskStatus.blocked
    assert before.tasks[task_id].block_reason == "boom"

    continue_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="retry")

    after = load_goal_run(str(tmp_path), goal_id)
    assert after is not None
    # Re-armed for the next attempt, with the failure kept as evidence.
    assert after.tasks[task_id].status == TaskStatus.pending
    assert after.tasks[task_id].block_reason == "boom"
    kinds = [event.get("type") for event in read_events(str(tmp_path), goal_id, limit=50)]
    assert "goal_run_pre_admitted" in kinds
    assert "pre_admission_reconciled" in kinds
    assert "goal_run_continued" in kinds


def test_bind_continuation_attaches_new_execution_id(tmp_path: Path) -> None:
    goal_id, _ = _failed_goal(tmp_path)
    continue_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="retry")
    state = bind_continuation_execution(
        project_root=str(tmp_path), goal_run_id=goal_id, execution_id="ex_second"
    )
    assert state.goal_id == goal_id
    assert state.execution_attempts[-1] == {
        "attempt": 1,
        "execution_id": "ex_second",
        "status": "running",
        "reason": "retry",
        "at": state.execution_attempts[-1]["at"],
    }
    kinds = [event.get("type") for event in read_events(str(tmp_path), goal_id, limit=50)]
    assert "goal_run_continuation_bound" in kinds


def test_completed_goal_cannot_continue_or_rebind(tmp_path: Path) -> None:
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="p5-done",
        session_id="p5-session",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "do it"}],
    )
    task_id = next(iter(pre.state.tasks))
    state = load_goal_run(str(tmp_path), pre.state.goal_id)
    assert state is not None
    state.tasks[task_id].acceptance = []
    from server.goal_run.store import save_goal_run

    save_goal_run(state, str(tmp_path))
    done = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=pre.state.goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="done",
    )
    assert done.status == GoalStatus.completed

    with pytest.raises(RuntimeError, match="cannot be continued"):
        continue_goal_run(project_root=str(tmp_path), goal_run_id=pre.state.goal_id)
    with pytest.raises(RuntimeError, match="no pending continuation attempt"):
        bind_continuation_execution(
            project_root=str(tmp_path), goal_run_id=pre.state.goal_id, execution_id="ex_dup"
        )
    # Nothing was appended and no execution was bound.
    assert load_goal_run(str(tmp_path), pre.state.goal_id).execution_attempts == []


def test_continue_unknown_goal_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="disappeared"):
        continue_goal_run(project_root=str(tmp_path), goal_run_id="goal_missing")
