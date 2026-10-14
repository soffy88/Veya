# 3O-IO-ALLOW — server/project_brain/patrol.py: Project Brain inspection and patrol runbook in .veya-project/
"""W16 Project Brain — Patrol runbook (§5).

Runs on routine fire (after _try_supervisor_handoff) when VEYA_PROJECT_BRAIN is on.
Runbook:
  1. 磁盘空间检查 (free >= 2GB; 低于阈值暂停派工并通知)
  2. 独立核验 (不信工人自报; 依据任务类型及简报规则跑核验器)
  3. 判定 (done / progress / stalled / failed)
  4. 动作 (stalled -> WIP保存+自动续跑; failed -> 记录fix_notes; done -> 审批收尾+结论发一次+关绑定)
  5. 沉默策略 (仅里程碑、需用户决定、连续3周期无进展主动汇报；其余沉默只写日志)
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import time
from typing import Any

from server.project_brain import enabled as project_brain_enabled
from server.project_brain.brief import load_brief
from server.project_brain.tasks import (
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    ProjectTask,
    get_task,
    list_tasks,
    mark_conclusion_sent,
    sync_tasks_to_brief,
    upsert_task,
)
from server.project_store import ProjectStore

_lg = logging.getLogger(__name__)

# Minimum free disk space in bytes (2 GB)
MIN_DISK_FREE_BYTES = 2 * 1024 * 1024 * 1024

# Consecutive no-progress cycles threshold (§11: 3 cycles)
NO_PROGRESS_THRESHOLD = 3


def check_disk_space(project_root: str) -> tuple[bool, int]:
    """Check if free disk space is at least MIN_DISK_FREE_BYTES.

    Returns (is_ok, free_bytes).
    """
    try:
        usage = shutil.disk_usage(project_root)
        return (usage.free >= MIN_DISK_FREE_BYTES, usage.free)
    except Exception:
        return (True, MIN_DISK_FREE_BYTES)


def _save_wip_snapshot(project_root: str, task_id: str) -> str:
    """Save worker WIP snapshot metadata without mutating repo history."""
    store = ProjectStore(project_root)
    run_dir = store.run_dir(task_id)
    snapshot_path = run_dir / "wip_snapshot.json"
    data = {
        "task_id": task_id,
        "saved_at": time.time(),
        "project_root": project_root,
    }
    with contextlib.suppress(Exception):
        from server.bot_team.conversations import atomic_write_json

        atomic_write_json(snapshot_path, data)
    return str(snapshot_path)


async def run_project_patrol(
    *,
    binding: Any,  # SupervisorBinding
    goal_run_id: str,
    project_root: str,
) -> dict[str, Any]:
    """Execute one iteration of the Project Brain patrol runbook."""
    if not project_brain_enabled():
        return {"verdict": "skipped", "reason": "flag_disabled"}

    # 1. 磁盘空间检查
    disk_ok, free_bytes = check_disk_space(project_root)
    if not disk_ok:
        free_mb = free_bytes // (1024 * 1024)
        from server.notification_center import global_notifier

        global_notifier.push(
            type="WARNING",
            title="[项目主脑] 磁盘空间不足",
            content=f"项目工作区磁盘剩余空间仅剩 {free_mb}MB（低于安全阈值 2GB），已暂停派工并通知。",
            payload={"free_bytes": free_bytes, "project_root": project_root},
            user_id=str(binding.owner_id),
        )
        store = ProjectStore(project_root)
        store.append_log(f"巡检暂停：磁盘剩余空间 ({free_mb}MB) 低于 2GB。")
        return {"verdict": "paused", "reason": "disk_space_low"}

    # Locate corresponding ProjectTask
    tasks = list_tasks(project_root)
    task: ProjectTask | None = None
    for t in tasks:
        if t.binding_id == binding.binding_id or t.goal_run_id == goal_run_id:
            task = t
            break

    if task is None:
        return {"verdict": "skipped", "reason": "no_project_task"}

    store = ProjectStore(project_root)
    brief = load_brief(project_root)

    # 2. 独立核验 (Independent Verification)
    from server.goal_run.models import GoalStatus
    from server.goal_run.store import load_goal_run

    goal_state = load_goal_run(project_root, goal_run_id)

    evidence_usable = False
    evidence_details = ""
    is_failed = False
    has_new_progress = False

    if goal_state is not None:
        total = len(goal_state.tasks)
        done = sum(1 for t in goal_state.tasks.values() if getattr(t.status, "value", str(t.status)) == "completed")
        failed = sum(1 for t in goal_state.tasks.values() if getattr(t.status, "value", str(t.status)) in ("blocked", "failed", "cancelled"))
        g_status = getattr(goal_state.status, "value", str(goal_state.status))
        if failed > 0 or g_status in ("blocked", "paused", "failed"):
            is_failed = True

        # Independent verification: leaf tasks must have concrete execution results,
        # not just worker claims
        if total > 0 and done == total and failed == 0:
            verified_results = [
                bool(t.execute_result and "error" not in t.execute_result.lower())
                for t in goal_state.tasks.values()
            ]
            if all(verified_results):
                evidence_usable = True
                evidence_details = f"独立核验通过：全部 {total} 个子任务均已执行且产物有效。"
            else:
                evidence_usable = False
                evidence_details = "核验未通过：子任务产物不完整或包含错误。"
        elif total > 0:
            has_error = any(
                bool(t.execute_result and "error" in t.execute_result.lower())
                for t in goal_state.tasks.values()
            )
            if has_error:
                evidence_usable = False
                evidence_details = "核验未通过：子任务产物包含错误。"
            else:
                evidence_usable = False
                evidence_details = f"执行中：已完成 {done}/{total} 个子任务。"

        # Check if progress advanced
        current_fp = f"{done}/{total}|{failed}"
        if getattr(binding, "last_report_fingerprint", "") != current_fp:
            has_new_progress = True
            binding.last_report_fingerprint = current_fp

    evidence = {
        "usable": evidence_usable,
        "details": evidence_details,
        "timestamp": time.time(),
    }
    task.verifier = evidence

    # 3. 判定 (Judgment: done / progress / stalled / failed / running)
    now = time.time()
    last_act = getattr(binding, "last_activity_at", None) or now
    stall_timeout = getattr(binding, "stall_after_minutes", 15) * 60
    is_stalled = (now - last_act > stall_timeout) and not evidence_usable

    if evidence_usable:
        verdict = "done"
    elif is_failed:
        verdict = "failed"
    elif is_stalled:
        verdict = "stalled"
    elif has_new_progress:
        verdict = "progress"
    else:
        verdict = "running"

    # 4. 动作 (Actions)
    from server.notification_center import global_notifier

    if verdict == "done":
        # 收尾动作 (合并/部署等) 走 ApprovalService (§11: 默认全部 ask)
        closing_actions_requested = False
        for rule in brief.rules:
            do_act = rule.do.lower()
            if any(k in do_act for k in ("merge", "deploy", "release", "合并", "部署")):
                # Create approval request
                from server.approvals import ApprovalService

                app_service = ApprovalService()
                app_req = await app_service.request(
                    tool=f"closing_{rule.do}",
                    args={"task_id": task.task_id, "rule": rule.to_dict()},
                    user_id=str(binding.owner_id),
                    reason=f"项目任务 [{task.task_id}] 完成收尾审批: {rule.do}",
                )
                closing_actions_requested = True
                global_notifier.push(
                    type="ACTION_REQUIRED",
                    title="[项目主脑] 收尾动作待审批",
                    content=f"任务 [{task.objective}] 独立核验已通过，收尾动作 [{rule.do}] 等待您的批准。",
                    payload={"approval_id": getattr(app_req, "approval_id", str(app_req)), "task_id": task.task_id},
                    user_id=str(binding.owner_id),
                )

        # 结论只发一次
        dedup_key = f"conclusion_{task.task_id}"
        rec = task.receipts.setdefault(dedup_key, {})
        if not rec.get("conclusion_sent", False):
            rec["conclusion_sent"] = True
            from server.bot_team.conversations import conversation_store

            conversation_store().append(
                str(binding.bot_id),
                role="bot",
                text=f"项目任务 [{task.task_id}] 已完成！\n- 目标：{task.objective}\n- 核验：{evidence_details}",
                meta={"source": "project_brain", "task_id": task.task_id, "status": "completed"},
            )
            global_notifier.push(
                type="SUCCESS",
                title="[项目主脑] 任务完成",
                content=f"项目任务 [{task.task_id}] 已成功完成并通过独立核验。",
                payload={"task_id": task.task_id, "goal_run_id": goal_run_id},
                user_id=str(binding.owner_id),
            )

        # 关闭 binding 与 routine
        from server.supervisor.handoff import _close_supervisor

        if getattr(binding, "status", "") != "closed":
            await _close_supervisor(binding, goal_run_id, reason="done")
        task.status = TASK_STATUS_DONE
        store.append_log(f"任务完成并通过独立核验 [{task.task_id}]: {task.objective}")

    elif verdict == "stalled":
        # 保存 WIP 快照
        _save_wip_snapshot(project_root, task.task_id)

        repairs_sent = getattr(binding, "nudges_sent", 0)
        max_repairs = getattr(binding, "max_repairs", 3)
        if repairs_sent < max_repairs:
            # 自动重起续跑
            if getattr(binding, "_patrol_nudged_seq", None) != getattr(binding, "report_seq", None):
                from server.supervisor.handoff import _emit_nudge

                await _emit_nudge(goal_run_id, binding)
                binding._patrol_nudged_seq = getattr(binding, "report_seq", None)
            task.fix_notes.append(f"巡检检测到停滞，自动重起续跑 (第 {repairs_sent + 1} 次)")
            store.append_log(f"任务停滞 [{task.task_id}]，自动重起 (第 {repairs_sent + 1}/{max_repairs} 次)")
        else:
            # 超过修复上限，通知用户决策
            global_notifier.push(
                type="WARNING",
                title="[项目主脑] 任务停滞需人工干预",
                content=f"任务 [{task.task_id}] 已连续修复 {max_repairs} 次仍无进展，已停止自动尝试，需人工介入。",
                payload={"task_id": task.task_id, "goal_run_id": goal_run_id},
                user_id=str(binding.owner_id),
            )
            from server.supervisor.handoff import _close_supervisor

            await _close_supervisor(binding, goal_run_id, reason="stalled_exhausted")
            task.status = TASK_STATUS_FAILED
            task.fix_notes.append(f"任务停滞已达修复上限 {max_repairs} 次，已停止。")
            store.append_log(f"任务停滞超限已终止 [{task.task_id}]。")

    elif verdict == "failed":
        task.status = TASK_STATUS_FAILED
        task.fix_notes.append("巡检检测到任务执行失败，记录 FIX 说明。")
        store.append_log(f"任务失败 [{task.task_id}]，已记录修复说明。")

    # 5. 沉默策略 (Silence Strategy)
    patrol_meta = task.receipts.setdefault("_patrol", {})
    if verdict in ("running", "progress") and not has_new_progress:
        no_progress_cycles = int(patrol_meta.get("no_progress_cycles") or 0) + 1
        patrol_meta["no_progress_cycles"] = no_progress_cycles
        if no_progress_cycles >= NO_PROGRESS_THRESHOLD:
            # 连续 3 周期无进展，主动通知
            global_notifier.push(
                type="INFO",
                title="[项目主脑] 持续无新进展",
                content=f"项目任务 [{task.task_id}] 已连续 {no_progress_cycles} 个巡检周期未见新进展。",
                payload={"task_id": task.task_id, "no_progress_cycles": no_progress_cycles},
                user_id=str(binding.owner_id),
            )
            patrol_meta["no_progress_cycles"] = 0
        else:
            # 保持沉默：不推送 global_notifier
            pass
    elif has_new_progress:
        patrol_meta["no_progress_cycles"] = 0

    # 记录本次巡检日志
    store.append_log(f"巡检周期执行完毕：判定={verdict} (usable={evidence_usable})")

    upsert_task(project_root, task)
    sync_tasks_to_brief(project_root)

    return {
        "verdict": verdict,
        "evidence": evidence,
        "task_id": task.task_id,
        "goal_run_id": goal_run_id,
    }
