"""W12 Supervisor — progress report builder (spec A3, A4, A5).

No new scheduler. Reports are built from the existing GoalRun state and
posted via the routine turn pipeline. The only state added is in the
binding record (server.supervisor.binding).
"""

from __future__ import annotations

import time
from typing import Any

from server.goal_run.models import GoalRunState, GoalStatus
from server.goal_run.store import load_goal_run


def build_progress_report(
    *,
    goal_run_id: str,
    project_root: str,
    binding: "SupervisorBinding",  # type: ignore[name-defined] — forward ref to avoid cycle
) -> dict[str, Any]:
    """Build a progress report from GoalRun state only.

    Returns a dict with keys:
      - goal_run_id, objective, status, report_seq, timestamp
      - tasks_done, tasks_total, tasks_blocked
      - last_activity_at, last_evidence
      - is_stalled, stall_reason
      - is_done
      - body: the human-readable report text

    Must not invent progress. If nothing changed since last report, say so.
    """
    from server.supervisor.binding import SupervisorBinding

    state = load_goal_run(project_root, goal_run_id)
    if state is None:
        return {
            "goal_run_id": goal_run_id,
            "body": "找不到该任务的执行状态。",
            "is_done": False,
            "is_stalled": False,
            "report_seq": binding.report_seq,
        }

    total = len(state.tasks)
    done = sum(1 for t in state.tasks.values() if t.status.value == "completed")
    blocked = sum(1 for t in state.tasks.values() if t.status.value == "blocked")
    running = sum(1 for t in state.tasks.values() if t.status.value == "running")

    # Last evidence: from the most recently completed task
    last_evidence = None
    for task in sorted(state.tasks.values(), key=lambda t: t.id):
        if task.status.value == "completed" and task.execute_result:
            last_evidence = task.execute_result[:200]
            break

    # Last activity: when was the last task completed/started
    now = time.time()
    last_activity = binding.last_activity_at or now
    stall_after_s = binding.stall_after_minutes * 60

    is_stalled = False
    stall_reason = None
    if state.status in (GoalStatus.running, GoalStatus.planning):
        if now - last_activity > stall_after_s:
            is_stalled = True
            stall_reason = f"任务已 {binding.stall_after_minutes} 分钟无活动"

    # Check if verification passed (all tasks completed)
    all_completed = total > 0 and done == total and blocked == 0
    is_done = all_completed or state.status in (GoalStatus.completed, GoalStatus.finalizing)

    # Build body
    if is_done:
        body = f"任务完成！已完成 {done}/{total} 个子任务。"
        if last_evidence:
            body += f"\n\n最新证据：{last_evidence[:100]}..."
    elif is_stalled:
        body = f"任务卡住了：{stall_reason}"
        if state.last_stop_reason:
            body += f"\n阻塞原因：{state.last_stop_reason}"
    elif done == 0:
        body = f"任务刚开始（{total} 个子任务），尚未完成任何子任务。"
    else:
        body = f"进度：已完成 {done}/{total} 个子任务，运行中 {running} 个，阻塞 {blocked} 个。"

    return {
        "goal_run_id": goal_run_id,
        "objective": binding.objective,
        "status": state.status.value,
        "report_seq": binding.report_seq,
        "timestamp": now,
        "tasks_done": done,
        "tasks_total": total,
        "tasks_blocked": blocked,
        "last_activity_at": last_activity,
        "last_evidence": last_evidence,
        "is_stalled": is_stalled,
        "stall_reason": stall_reason,
        "is_done": is_done,
        "body": body,
    }
