"""W17: Supervisor bridge for remote evidence.

Feeds remote task execution evidence into W12 supervisor reports
by updating GoalRun task state.
"""

from __future__ import annotations

from typing import Any

from server.goal_run.models import TaskNode, TaskStatus
from server.goal_run.store import load_goal_run, save_goal_run
from server.remote_control import sessions as sessions_mod
from server.remote_control.sessions import VerifierEvidence


def inject_remote_evidence(
    *,
    project_root: str,
    goal_run_id: str,
    evidence: VerifierEvidence | Any,
    task_id: str = "remote_task",
) -> bool:
    """Inject remote evidence into a GoalRun task node so W12 reporting reflects it."""
    state = load_goal_run(project_root, goal_run_id)
    if state is None:
        return False

    summary = str(getattr(evidence, "summary", "") or evidence or "")
    verified = bool(getattr(evidence, "verified", False))

    if task_id in state.tasks:
        task = state.tasks[task_id]
        task.execute_result = summary
        if verified:
            task.status = TaskStatus.completed
        else:
            task.status = TaskStatus.running
    else:
        task = TaskNode(
            id=task_id,
            title=f"Remote task {task_id}",
            instruction="Execute remote task",
            acceptance=["Remote task finished with exit 0"],
            depends_on=[],
            assignee="remote",
            status=TaskStatus.completed if verified else TaskStatus.running,
            execute_result=summary,
        )
        state.tasks[task_id] = task

    save_goal_run(state, project_root)
    return True


def bridge_bg_status_to_supervisor(
    *,
    project_root: str,
    goal_run_id: str,
    bg_status: dict[str, Any],
    task_id: str = "remote_task",
) -> VerifierEvidence:
    """Turn a background status dict into VerifierEvidence and inject into supervisor GoalRun."""
    evidence = sessions_mod.evidence_from_bg_status(bg_status)
    inject_remote_evidence(
        project_root=project_root,
        goal_run_id=goal_run_id,
        evidence=evidence,
        task_id=task_id,
    )
    return evidence


__all__ = [
    "bridge_bg_status_to_supervisor",
    "inject_remote_evidence",
]
