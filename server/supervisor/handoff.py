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
        type="WARNING" if report["is_stalled"] else "INFO" if report["status"] == "completed" else "SUCCESS",
        title=f"[监工] {report['status']}",
        content=report["body"],
        payload={"goal_run_id": goal_run_id, "report_seq": report["report_seq"]},
        user_id=str(binding.owner_id),
    )

    # Update last_activity_at so the next report interval is measured fresh.
    if goal_state is not None:
        now = time.time()
        binding.last_activity_at = now
        from server.supervisor import binding as bind_mod
        bind_mod.save_binding(binding)

    # A4: if stalled and under max_repairs, emit a HEARTBEAT nudge.
    if report["is_stalled"] and report["repairs_remaining"] > 0:
        await _emit_nudge(goal_run_id, binding)

    # A5: verifier-passed → close the binding and delete the routine.
    if report["is_done"]:
        await _close_supervisor(binding, goal_run_id)


async def _close_supervisor(binding: SupervisorBinding, goal_run_id: str) -> None:
    """Mark the binding closed and remove its routine. Runs on every
    ``is_done`` report, so it is idempotent — the binding is only
    reopened by ``supervise_task``.
    """
    from server.goal_run.routine_registry import RoutineRegistry
    from server.supervisor import binding as bind_mod

    binding.status = "closed"
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

    global_notifier.push(
        type="SUCCESS",
        title="[监工] 任务完成",
        content=f"任务 {goal_run_id} 已完成。",
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
