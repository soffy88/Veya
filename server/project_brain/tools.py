# 3O-IO-ALLOW — server/project_brain/tools.py: Project Brain model tools (Brief, Tasks, Logs) in .veya-project/
"""W16 Project Brain — model-callable tools (§7).

All tools are gated on VEYA_PROJECT_BRAIN and derive the project scope from
``current_bot_id()`` + ``BotRegistry`` (isolation rule: models cannot name a project_id).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any

from server.goal_run.bot_identity import BotRegistry, current_bot_id
from server.project_brain import enabled as project_brain_enabled
from server.project_brain.brief import (
    load_brief,
    render_brief,
    update_brief_section,
)
from server.project_brain.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_RUNNING,
    ProjectTask,
    get_task,
    list_tasks,
    sync_tasks_to_brief,
    upsert_task,
)
from server.project_store import ProjectStore


def _resolve_project() -> tuple[str, str]:
    """Resolve caller's (bot_id, project_root).

    Enforces §8 isolation: project_id is never a model argument.
    """
    bot_id = current_bot_id()
    if not bot_id:
        raise PermissionError("无法识别当前机器人身份，拒绝访问项目工具。")
    spec = BotRegistry().get(bot_id)
    if spec is None:
        raise PermissionError(f"机器人 {bot_id} 未在注册表登记。")
    root = getattr(spec, "workspace_path", "") or getattr(spec, "default_workspace_id", "")
    if not root:
        raise PermissionError(f"机器人 {bot_id} 未绑定项目工作区 (workspace_path 为空)。")
    return bot_id, str(root)


async def project_brief_read(section: str = "") -> str:
    """Read the current project brief, or a specific section (PURE_READ)."""
    if not project_brain_enabled():
        return "已拒绝：项目主脑未启用（flag VEYA_PROJECT_BRAIN=0）。"
    try:
        _, project_root = _resolve_project()
    except PermissionError as exc:
        return f"已拒绝：{exc}"

    brief = load_brief(project_root)
    sec = section.strip().lower()
    if not sec or sec == "all":
        return render_brief(brief)
    if sec in ("goal", "scope", "goal_scope", "1", "目标", "范围", "目标与范围"):
        return brief.goal_scope or "暂无目标描述。"
    if sec in ("key_files", "files", "2", "重要文件", "文件"):
        return "\n".join(f"- {f}" for f in brief.key_files) if brief.key_files else "暂无"
    if sec in ("rules", "3", "规则"):
        return "\n".join(r.render() for r in brief.rules) if brief.rules else "暂无"
    if sec in ("active_tasks", "tasks", "4", "进行中任务与阻塞", "任务"):
        return brief.active_tasks or "暂无进行中任务。"
    if sec in ("conventions", "decisions", "5", "约定", "决策"):
        return brief.conventions or "暂无约定。"
    return f"未知简报章节: {section}"


async def project_brief_update(section: str, content: str) -> str:
    """Update a specific section in the project brief (LOCAL_WRITE)."""
    if not project_brain_enabled():
        return "已拒绝：项目主脑未启用（flag VEYA_PROJECT_BRAIN=0）。"
    try:
        _, project_root = _resolve_project()
    except PermissionError as exc:
        return f"已拒绝：{exc}"

    update_brief_section(project_root, section, content)
    store = ProjectStore(project_root)
    store.append_log(f"更新项目简报章节 [{section}]")
    return f"已成功更新项目简报章节 [{section}]。"


async def project_task_start(
    objective: str,
    report_every_minutes: int = 5,
    stall_after_minutes: int = 15,
    message_id: str = "",
) -> str:
    """Decompose and start a supervised project task (PROCESS_EXEC)."""
    if not project_brain_enabled():
        return "已拒绝：项目主脑未启用（flag VEYA_PROJECT_BRAIN=0）。"
    try:
        bot_id, project_root = _resolve_project()
    except PermissionError as exc:
        return f"已拒绝：{exc}"

    from server.supervision_tools import supervise_task

    try:
        res = supervise_task(
            project_root=project_root,
            objective=objective,
            report_every_minutes=int(report_every_minutes),
            stall_after_minutes=int(stall_after_minutes),
            bot_id=bot_id,
        )
    except Exception as exc:
        return f"启动任务失败: {exc}"

    task_id = f"ptask_{uuid.uuid4().hex[:8]}"
    binding_info = res.get("binding") or {}
    binding_id = (
        binding_info.get("binding_id")
        if isinstance(binding_info, dict)
        else getattr(binding_info, "binding_id", "")
    )
    goal_run_id = res.get("goal_run_id") or ""
    routine_id = res.get("routine_id") or ""

    task = ProjectTask(
        task_id=task_id,
        objective=objective,
        goal_run_id=goal_run_id,
        binding_id=binding_id,
        routine_id=routine_id,
        status=TASK_STATUS_RUNNING,
    )
    if message_id:
        task.receipts[message_id] = {"receipt_sent": True, "conclusion_sent": False}
    upsert_task(project_root, task)
    sync_tasks_to_brief(project_root)

    store = ProjectStore(project_root)
    store.append_log(f"启动项目任务 [{task_id}]: {objective} (GoalRun={goal_run_id}, Binding={binding_id})")

    return (
        f"已启动项目任务 [{task_id}]：{objective}。\n"
        f"- 范围说明：将在项目工作区内拆解执行目标，每 {report_every_minutes} 分钟独立核验并汇报进度，"
        f"超时 {stall_after_minutes} 分钟无进展将自动尝试恢复。\n"
        f"- 关联 GoalRun: {goal_run_id}，巡检绑定: {binding_id}。"
    )


async def project_task_status(task_id: str = "") -> str:
    """Read the status of a project task without triggering execution (PURE_READ)."""
    if not project_brain_enabled():
        return "已拒绝：项目主脑未启用（flag VEYA_PROJECT_BRAIN=0）。"
    try:
        _, project_root = _resolve_project()
    except PermissionError as exc:
        return f"已拒绝：{exc}"

    if task_id:
        task = get_task(project_root, task_id)
        if not task:
            return f"未找到任务 {task_id}。"
    else:
        tasks = list_tasks(project_root)
        if not tasks:
            return "当前项目暂无任务记录。"
        task = tasks[0]

    report_text = ""
    if task.binding_id and task.goal_run_id:
        from server.supervisor import binding as supervisor_binding

        b = supervisor_binding.get_binding(task.binding_id)
        if b:
            from server.supervisor.reporting import build_progress_report

            rep = build_progress_report(
                goal_run_id=task.goal_run_id,
                project_root=project_root,
                binding=b,
                verifier_evidence=task.verifier,
            )
            report_text = rep.get("body", "")

    notes_str = f"\n修复说明: {'; '.join(task.fix_notes)}" if task.fix_notes else ""
    rep_str = f"\n最新报告: {report_text}" if report_text else ""
    return (
        f"任务 [{task.task_id}] 状态: {task.status}\n"
        f"目标: {task.objective}"
        f"{rep_str}"
        f"{notes_str}"
    )


async def project_task_steer(task_id: str, steer_content: str) -> str:
    """Inject a user steer / correction to a running task (LOCAL_WRITE)."""
    if not project_brain_enabled():
        return "已拒绝：项目主脑未启用（flag VEYA_PROJECT_BRAIN=0）。"
    try:
        bot_id, project_root = _resolve_project()
    except PermissionError as exc:
        return f"已拒绝：{exc}"

    task = get_task(project_root, task_id)
    if not task:
        return f"未找到任务 {task_id}。"

    # 1. Post to agent mailbox
    from server.goal_run.agent_mailbox import AgentMailbox, DeliveryMode

    try:
        mailbox = AgentMailbox(project_root=project_root)
        mailbox.send(
            sender_id=bot_id,
            recipient_id=task.goal_run_id or bot_id,
            body={"steer": steer_content, "task_id": task_id},
            mode=DeliveryMode.STEER,
        )
    except Exception:
        pass

    # 2. Inject to turn queue if active
    with contextlib.suppress(Exception):
        from server.nano_w10.turn_queue import get_turn_queue, turn_queue_enabled

        if turn_queue_enabled():
            queue = get_turn_queue()
            queue.push(bot_id, f"用户插话修正: {steer_content}")

    # 3. Record in task fix notes and project log
    task.fix_notes.append(f"用户修正: {steer_content}")
    upsert_task(project_root, task)

    store = ProjectStore(project_root)
    store.append_log(f"用户插话修正 [{task_id}]: {steer_content}")

    return f"已成功将修正指令投递至任务 [{task_id}]。"


async def project_task_cancel(task_id: str, reason: str = "") -> str:
    """Cancel a running project task and close its supervisor binding (LOCAL_WRITE)."""
    if not project_brain_enabled():
        return "已拒绝：项目主脑未启用（flag VEYA_PROJECT_BRAIN=0）。"
    try:
        bot_id, project_root = _resolve_project()
    except PermissionError as exc:
        return f"已拒绝：{exc}"

    task = get_task(project_root, task_id)
    if not task:
        return f"未找到任务 {task_id}。"

    # Close supervisor binding and routine
    if task.binding_id:
        from server.supervisor import binding as supervisor_binding

        b = supervisor_binding.get_binding(task.binding_id)
        if b:
            b.status = "closed"
            supervisor_binding.save_binding(b)
            if b.routine_id:
                from server.goal_run.routine_registry import RoutineRegistry

                with contextlib.suppress(Exception):
                    RoutineRegistry(bot_id=bot_id).set_enabled(b.routine_id, False)

    # Pause GoalRun
    if task.goal_run_id:
        from server.goal_run.models import GoalStatus
        from server.goal_run.store import load_goal_run, save_goal_run

        gstate = load_goal_run(project_root, task.goal_run_id)
        if gstate:
            gstate.status = GoalStatus.paused
            save_goal_run(gstate, project_root)

    task.status = TASK_STATUS_CANCELLED
    if reason:
        task.fix_notes.append(f"取消原因: {reason}")
    upsert_task(project_root, task)
    sync_tasks_to_brief(project_root)

    store = ProjectStore(project_root)
    store.append_log(f"取消任务 [{task_id}]: {reason or '用户取消'}")

    return f"任务 [{task_id}] 已取消并关闭巡检绑定。"


async def project_log_append(entry: str) -> str:
    """Append an entry to the project daily log (LOCAL_WRITE)."""
    if not project_brain_enabled():
        return "已拒绝：项目主脑未启用（flag VEYA_PROJECT_BRAIN=0）。"
    try:
        _, project_root = _resolve_project()
    except PermissionError as exc:
        return f"已拒绝：{exc}"

    store = ProjectStore(project_root)
    store.append_log(entry)
    preview = entry.strip().replace("\n", " ")[:50]
    return f"已记录项目日志：{preview}..."


def register_tools(registry: Any) -> int:
    """Register Project Brain tools into master_tools (gated on VEYA_PROJECT_BRAIN)."""
    if not project_brain_enabled():
        return 0
    if registry.has("project_brief_read"):
        return 0

    from server.tool_registry import SideEffect

    count = 0

    registry.register(
        "project_brief_read",
        "读取当前项目的简报内容（目标/范围、重要文件、规则、进行中任务、约定）。只读。",
        {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "可选章节名: goal_scope | key_files | rules | active_tasks | conventions，留空为全部。",
                }
            },
        },
        project_brief_read,
        side_effect=SideEffect.PURE_READ,
    )
    count += 1

    registry.register(
        "project_brief_update",
        "修改当前项目的简报章节。只限本项目 .veya-project 目录。",
        {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "章节名: goal_scope | key_files | rules | active_tasks | conventions。",
                },
                "content": {
                    "type": "string",
                    "description": "更新内容文本。",
                },
            },
            "required": ["section", "content"],
        },
        project_brief_update,
        side_effect=SideEffect.LOCAL_WRITE,
    )
    count += 1

    registry.register(
        "project_task_start",
        "在当前项目中拆解并启动一个受监工巡检的长任务，返回任务范围说明。自动绑定巡检与定期汇报。",
        {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "要完成的任务目标与要求。",
                },
                "report_every_minutes": {
                    "type": "integer",
                    "description": "汇报巡检周期（分钟），默认 5 分钟。",
                },
                "stall_after_minutes": {
                    "type": "integer",
                    "description": "无进展卡住超时（分钟），默认 15 分钟。",
                },
                "message_id": {
                    "type": "string",
                    "description": "可选触发消息 ID，用于建立回执与结论去重记录。",
                },
            },
            "required": ["objective"],
        },
        project_task_start,
        side_effect=SideEffect.PROCESS_EXEC,
    )
    count += 1

    registry.register(
        "project_task_status",
        "查询当前项目任务的执行进度与最新报告。只读，不触发新执行。",
        {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "可选任务 ID，留空查询最近一个任务。",
                }
            },
        },
        project_task_status,
        side_effect=SideEffect.PURE_READ,
    )
    count += 1

    registry.register(
        "project_task_steer",
        "向正在运行的项目任务投递用户的插话修正指令，使工人能够中途调整。",
        {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "正在运行的任务 ID。",
                },
                "steer_content": {
                    "type": "string",
                    "description": "用户的插话修正内容。",
                },
            },
            "required": ["task_id", "steer_content"],
        },
        project_task_steer,
        side_effect=SideEffect.LOCAL_WRITE,
    )
    count += 1

    registry.register(
        "project_task_cancel",
        "取消正在运行的项目任务，并关闭监工巡检绑定。",
        {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "任务 ID。",
                },
                "reason": {
                    "type": "string",
                    "description": "可选取消原因。",
                },
            },
            "required": ["task_id"],
        },
        project_task_cancel,
        side_effect=SideEffect.LOCAL_WRITE,
    )
    count += 1

    registry.register(
        "project_log_append",
        "向当前项目的每日日志 (log/YYYY-MM-DD.md) 追加一条记录。",
        {
            "type": "object",
            "properties": {
                "entry": {
                    "type": "string",
                    "description": "日志文本。",
                }
            },
            "required": ["entry"],
        },
        project_log_append,
        side_effect=SideEffect.LOCAL_WRITE,
    )
    count += 1

    return count
