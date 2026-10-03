"""P6 GoalRun recovery: RUNNING -> RECOVERING -> RECOVERED, deterministically.

Acceptance: an execution killed mid-flight loses nothing on restart; resume
is idempotent; recovery completes only when the reloaded run verifies clean;
a completed goal can never resume; the same dispatch never forks a duplicate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.goal_run.models import GoalStatus, TaskStatus
from server.goal_run.pre_admission import (
    complete_recovery,
    continue_goal_run,
    create_pending_run,
    fail_pre_admission,
    load_goal_run,
    mark_task_running,
    reconcile_task,
    resume_goal_run,
)
from server.goal_run.store import read_events, save_goal_run


def _running_goal(tmp_path: Path) -> tuple[str, str]:
    """A run killed mid-execution: admitted, bound, then left RUNNING."""
    pre = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="p6-dispatch",
        session_id="p5-session",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "do it"}],
    )
    task_id = next(iter(pre.state.tasks))
    mark_task_running(
        project_root=str(tmp_path), goal_run_id=pre.state.goal_id, goal_task_id=task_id
    )
    return pre.state.goal_id, task_id


def test_killed_run_loses_nothing_on_reload(tmp_path: Path) -> None:
    goal_id, task_id = _running_goal(tmp_path)
    # The crash analogue: a cold reload straight from disk.
    reloaded = load_goal_run(str(tmp_path), goal_id)
    assert reloaded is not None
    assert reloaded.goal_id == goal_id
    assert reloaded.status == GoalStatus.running
    assert reloaded.tasks[task_id].status == TaskStatus.running
    assert reloaded.dispatch_id == "p6-dispatch"
    assert reloaded.requested_executor == "codex"


def test_resume_marks_recovering_and_is_idempotent(tmp_path: Path) -> None:
    goal_id, _ = _running_goal(tmp_path)
    resumed = resume_goal_run(
        project_root=str(tmp_path), goal_run_id=goal_id, reason="restart detected"
    )
    assert resumed.status == GoalStatus.recovering
    again = resume_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="again")
    assert again.status == GoalStatus.recovering
    kinds = [event.get("type") for event in read_events(str(tmp_path), goal_id, limit=50)]
    assert "goal_run_recovery_started" in kinds
    assert "goal_run_pre_admitted" in kinds


def test_recovery_completes_only_when_clean(tmp_path: Path) -> None:
    goal_id, _task_id = _running_goal(tmp_path)
    resume_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="restart")
    # Tasks still open: recovery cannot close, continuation is the way out.
    with pytest.raises(RuntimeError, match="unfinished tasks"):
        complete_recovery(project_root=str(tmp_path), goal_run_id=goal_id)
    continued = continue_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="retry")
    assert continued["attempt"] == 1


def test_recovered_terminal_without_new_work(tmp_path: Path) -> None:
    goal_id, task_id = _running_goal(tmp_path)
    state = load_goal_run(str(tmp_path), goal_id)
    assert state is not None
    state.tasks[task_id].acceptance = []
    save_goal_run(state, str(tmp_path))
    resume_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="restart")
    # The worker had actually finished before the crash: reconcile first.
    # The run entered reconciliation from recovery, so it closes as
    # recovered (verified clean, no new work), not as a fresh completion.
    reconciled = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="done",
    )
    assert reconciled.tasks[task_id].status == TaskStatus.completed
    assert reconciled.status == GoalStatus.recovered
    assert reconciled.acceptance_verdict == "ACCEPT"
    kinds = [event.get("type") for event in read_events(str(tmp_path), goal_id, limit=50)]
    assert "goal_run_recovery_started" in kinds
    # Spec flow: verification of the recovered run lands COMPLETED.
    final = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="done",
    )
    assert final.status == GoalStatus.completed
    # Terminal: no further resume, no continuation.
    with pytest.raises(RuntimeError, match="cannot resume"):
        resume_goal_run(project_root=str(tmp_path), goal_run_id=goal_id)
    with pytest.raises(RuntimeError, match="cannot be continued"):
        continue_goal_run(project_root=str(tmp_path), goal_run_id=goal_id)


def test_no_duplicate_goal_on_same_dispatch(tmp_path: Path) -> None:
    first = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="p6-once",
        session_id="s",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "do it"}],
    )
    fail_pre_admission(project_root=str(tmp_path), goal_run_id=first.state.goal_id, reason="x")
    resume_goal_run(project_root=str(tmp_path), goal_run_id=first.state.goal_id, reason="r")
    replay = create_pending_run(
        project_root=str(tmp_path),
        dispatch_id="p6-once",
        session_id="s",
        requested_executor="codex",
        tasks=[{"worker": "codex", "task": "do it"}],
    )
    assert replay.state.goal_id == first.state.goal_id


def test_completed_goal_cannot_resume(tmp_path: Path) -> None:
    goal_id, task_id = _running_goal(tmp_path)
    state = load_goal_run(str(tmp_path), goal_id)
    assert state is not None
    state.tasks[task_id].acceptance = []
    save_goal_run(state, str(tmp_path))
    done = reconcile_task(
        project_root=str(tmp_path),
        goal_run_id=goal_id,
        goal_task_id=task_id,
        outcome="completed",
        summary="done",
    )
    assert done.status == GoalStatus.completed
    with pytest.raises(RuntimeError, match="cannot resume"):
        resume_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="too late")


def test_complete_recovery_closes_clean_reloaded_run(tmp_path: Path) -> None:
    """complete_recovery covers tasks already completed at resume time."""
    goal_id, task_id = _running_goal(tmp_path)
    resume_goal_run(project_root=str(tmp_path), goal_run_id=goal_id, reason="restart")
    # Task-level completion persisted without goal aggregation (crash
    # between the two writes, or an external task-level close-out).
    state = load_goal_run(str(tmp_path), goal_id)
    assert state is not None
    state.tasks[task_id].status = TaskStatus.completed
    save_goal_run(state, str(tmp_path))
    recovered = complete_recovery(project_root=str(tmp_path), goal_run_id=goal_id)
    assert recovered.status == GoalStatus.recovered
    kinds = [event.get("type") for event in read_events(str(tmp_path), goal_id, limit=50)]
    assert "goal_run_recovered" in kinds
