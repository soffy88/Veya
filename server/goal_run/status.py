"""goal_run status — project_goal_status 只读查询。

返回 taskgraph 摘要 + 最近 events 尾部。不调度、不执行代码、不修改任何文件。
"""

from server.goal_run.models import GoalRunResponse, GoalStatus
from server.goal_run.store import get_goal_run_artifacts, load_goal_run


async def project_goal_status(
    project_root: str,
    goal_id: str | None = None,
) -> GoalRunResponse:
    """查询 goal_run 状态。

    - 若 goal_id 为空：返回最近一个 run 的状态
    - 若指定 goal_id：返回该 run 的状态
    - 不调度、不执行、不修改
    """
    from pathlib import Path

    # 查找 goal_id
    if goal_id is None:
        # 自动查找最近的 goal run
        runs_dir = Path(project_root) / ".veya-project" / "goal-runs"
        if not runs_dir.exists():
            return GoalRunResponse(
                goal_id="",
                status=GoalStatus.blocked,
                phase="rejected",
                interpretation=None,
                questions=None,
                goal_counts=None,
                summary=None,
                block_reason="no goal runs found",
                artifacts=None,
                next_action="none",
            )

        goal_dirs = sorted(runs_dir.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)
        if not goal_dirs:
            return GoalRunResponse(
                goal_id="",
                status=GoalStatus.blocked,
                phase="rejected",
                interpretation=None,
                questions=None,
                goal_counts=None,
                summary=None,
                block_reason="no goal runs found",
                artifacts=None,
                next_action="none",
            )

        goal_id = goal_dirs[0].name

    # 加载状态
    state = load_goal_run(project_root, goal_id)
    if state is None:
        return GoalRunResponse(
            goal_id=goal_id,
            status=GoalStatus.blocked,
            phase="rejected",
            interpretation=None,
            questions=None,
            goal_counts=None,
            summary=None,
            block_reason=f"goal run {goal_id} not found",
            artifacts=None,
            next_action="none",
        )

    # 统计任务数
    pending = sum(1 for tn in state.tasks.values() if tn.status.name == "pending")
    running = sum(1 for tn in state.tasks.values() if tn.status.name == "running")
    completed = sum(1 for tn in state.tasks.values() if tn.status.name == "completed")
    blocked = sum(1 for tn in state.tasks.values() if tn.status.name == "blocked")
    cancelled = sum(1 for tn in state.tasks.values() if tn.status.name == "cancelled")
    partial = sum(
        1
        for tn in state.tasks.values()
        if (
            tn.unfinished_work
            or tn.evidence
            or tn.assertions
            or (tn.delegate_result or {}).get("status") in {"partial", "failed", "paused", "cancelled"}
        )
    )

    # 确定 next_action
    if state.status in (GoalStatus.running, GoalStatus.recovering, GoalStatus.finalizing):
        next_action = "wait"
    elif state.status == GoalStatus.awaiting_user:
        next_action = "answer_clarification"
    elif state.status == GoalStatus.blocked:
        next_action = "inspect_tasks"
    elif state.status in (GoalStatus.completed, GoalStatus.partial_completed, GoalStatus.failed):
        next_action = "none"
    else:
        next_action = "wait"

    return GoalRunResponse(
        goal_id=goal_id,
        status=state.status,
        phase=state.status.value,  # 简化：phase 直接用 status 值
        interpretation=state.tasks[next(iter(state.tasks))].instruction if state.tasks else None,
        questions=None,
        goal_counts={
            "pending": pending,
            "running": running,
            "completed": completed,
            "blocked": blocked + cancelled,
            "cancelled": cancelled,
            "partial": partial,
        },
        summary=state.final_summary,
        block_reason=state.tasks[next(iter(state.tasks))].block_reason
        if state.tasks and state.tasks[next(iter(state.tasks))].block_reason
        else None,
        artifacts=get_goal_run_artifacts(project_root, goal_id),
        next_action=next_action,
    )
