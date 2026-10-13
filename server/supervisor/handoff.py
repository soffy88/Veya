"""W12 Supervisor handoff — turns routine-fire events into progress reports.

When a supervisor routine fires (trigger_topic == "schedule.trigger"), this
module builds a progress report from the current GoalRun state and posts it
into the owning bot's conversation. If the GoalRun is stalled (no activity
for longer than ``stall_after_minutes``), it also emits a HEARTBEAT continuation
trigger so the running task gets nudged.

This keeps A3 (report posting) and A4 (stall nudge) without polluting the
existing bot-turn pipeline.
"""

from __future__ import annotations

import logging
import time

from server.supervisor.binding import SupervisorBinding
from server.supervisor.reporting import build_progress_report

_lg = logging.getLogger(__name__)


async def supervisor_handoff(
    *,
    binding: SupervisorBinding,
    goal_run_id: str,
    project_root: str,
) -> None:
    """Post a progress report (and possibly a stall nudge) for a supervisor run.

    Always updates ``binding.last_activity_at`` to now so the next report
    sees fresh activity regardless of whether anything actually changed.
    """
    from server.goal_run.store import load_goal_run
    from server.notification_center import global_notifier

    goal_state = load_goal_run(project_root, goal_run_id)

    # A7: advance report_seq BEFORE building the report so every posted
    # report carries a unique, monotonic id. A restart never replays the
    # same seq because the persisted binding has already moved on.
    binding.report_seq = binding.report_seq + 1
    binding.last_report_at = time.time()
    from server.supervisor import binding as bind_mod

    bind_mod.save_binding(binding)

    report = build_progress_report(
        goal_run_id=goal_run_id,
        project_root=project_root,
        binding=binding,
    )

    # Post the report as a supervisor message in the bot's conversation.
    try:
        from server.bot_team.conversations import conversation_store

        conversation_store().append(
            str(binding.bot_id),
            role="bot",
            text=report["body"],
            meta={
                "source": "supervisor",
                "goal_run_id": goal_run_id,
                "report_seq": report["report_seq"],
            },
        )
    except Exception:
        _lg.debug("could not append supervisor report to conversation", exc_info=True)

    global_notifier.push(
        type="SUCCESS" if report["status"] == "completed" else "INFO",
        title=f"[监工] {report['status']}",
        content=report["body"],
        payload={"goal_run_id": goal_run_id, "report_seq": report["report_seq"]},
        user_id=str(binding.owner_id),
    )

    # Activity = the run's observable progress changed since the last report
    # (funcfix: this used to be reset to "now" on every report, so a run that
    # never started looked alive and one that did looked stalled at random).
    if goal_state is not None:
        fingerprint = "|".join(
            str(report.get(k)) for k in ("status", "tasks_done", "tasks_total", "tasks_blocked")
        ) + f"|{getattr(goal_state, 'started_at', None)}"
        if fingerprint != binding.last_report_fingerprint or binding.last_activity_at is None:
            binding.last_activity_at = time.time()
            binding.last_report_fingerprint = fingerprint
        from server.supervisor import binding as bind_mod
        bind_mod.save_binding(binding)

    # A4: if stalled and under max_repairs, emit a HEARTBEAT nudge.
    if report["is_stalled"] and report["repairs_remaining"] > 0:
        await _emit_nudge(goal_run_id, binding)
    elif report["is_stalled"] and not report["is_done"]:
        # funcfix: repairs exhausted. Stop instead of reporting "stuck" forever.
        await _close_supervisor(
            binding,
            goal_run_id,
            reason="gave_up",
            message=(
                f"监工已停止：任务 {goal_run_id} 已自动续跑 {binding.nudges_sent} 次仍无进展。"
                "需要的话请重新发起。"
            ),
        )
        return
    elif (
        goal_state is not None
        and not report["is_done"]
        and getattr(goal_state, "is_terminal", lambda: False)()
    ):
        # funcfix: the run ended without passing (blocked / failed /
        # partial_completed / cancelled). It will never become "done", so the
        # binding used to stay open and report the same state forever.
        status_value = getattr(goal_state.status, "value", str(goal_state.status))
        stop_reason = str(getattr(goal_state, "last_stop_reason", "") or "").strip()
        await _close_supervisor(
            binding,
            goal_run_id,
            reason=status_value,
            message=(
                f"监工已停止：任务 {goal_run_id} 已结束（{status_value}）。"
                + (f"\n原因：{stop_reason[:300]}" if stop_reason else "")
            ),
        )
        return
    elif goal_state is None and binding.report_seq >= 2:
        await _close_supervisor(
            binding,
            goal_run_id,
            reason="missing",
            message=f"监工已停止：找不到任务 {goal_run_id} 的执行状态。",
        )
        return

    # A5: verifier-passed → close the binding and delete the routine.
    if report["is_done"]:
        await _close_supervisor(binding, goal_run_id)


async def _close_supervisor(
    binding: SupervisorBinding,
    goal_run_id: str,
    *,
    reason: str = "done",
    message: str | None = None,
) -> None:
    """Mark the binding closed and remove its routine. Runs on every
    ``is_done`` report, so it is idempotent — the binding is only
    reopened by ``supervise_task``.
    """
    from server.goal_run.routine_registry import RoutineRegistry
    from server.supervisor import binding as bind_mod

    binding.status = "closed"
    if hasattr(binding, "closed_reason"):
        binding.closed_reason = reason
    try:
        bind_mod.save_binding(binding)
    except Exception:
        _lg.debug("could not save closed binding", exc_info=True)

    # Delete the routine so it stops firing.
    try:
        registry = RoutineRegistry(bot_id=str(binding.bot_id))
        if binding.routine_id and registry.get(binding.routine_id) is not None:
            # Best-effort: the RoutineRegistry does not expose a delete, so
            # flip enabled=False to stop dispatch. A restart would still
            # surface the routine, but bindings_for_goal excludes closed ones.
            registry.set_enabled(binding.routine_id, False)
    except Exception:
        _lg.debug("could not disable supervisor routine", exc_info=True)

    from server.notification_center import global_notifier

    if message:
        try:
            from server.bot_team.conversations import conversation_store

            conversation_store().append(
                str(binding.bot_id),
                role="bot",
                text=message,
                meta={"source": "supervisor", "goal_run_id": goal_run_id, "closed": reason},
            )
        except Exception:
            _lg.debug("could not append supervisor close message", exc_info=True)
    global_notifier.push(
        type="SUCCESS" if reason == "done" else "INFO",
        title="[监工] 任务完成" if reason == "done" else "[监工] 已停止",
        content=message or f"任务 {goal_run_id} 已完成。",
        payload={"goal_run_id": goal_run_id, "binding_id": binding.binding_id},
        user_id=str(binding.owner_id),
    )


async def _emit_nudge(goal_run_id: str, binding: SupervisorBinding) -> None:
    """Create a one-shot HEARTBEAT continuation trigger to nudge the running task.

    Increments ``binding.nudges_sent`` so the count is persisted across
    restarts and the max_repairs cap can be enforced.
    """
    from server.goal_run.continuation import (
        ContinuationTriggerManager,
        TriggerType,
    )
    from server.supervisor import binding as bind_mod

    mgr = ContinuationTriggerManager()
    # Use a deterministic trigger id per goal so we don't stack nudges.
    trigger_id = f"nudge_{goal_run_id}"
    existing = mgr.store.get(trigger_id)
    if existing is not None and existing.status.name != "FIRED":
        # Already a pending nudge — don't stack.
        return
    mgr.create_trigger(
        goal_run_id=goal_run_id,
        trigger_type=TriggerType.HEARTBEAT,
        payload={"source": "supervisor", "binding_id": binding.binding_id},
    )
    binding.nudges_sent = binding.nudges_sent + 1
    try:
        bind_mod.save_binding(binding)
    except Exception:
        _lg.debug("could not persist nudges_sent", exc_info=True)
