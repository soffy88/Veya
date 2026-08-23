"""goal_run runner — project_run_goal 主流程。

实现 v0.1 Spec 完整长时闭环：
G0 Understand → G1 Plan → G2 Loop (调度 + 执行 + 验收) → G3 Finalize。

硬约束：Coordinator 不靠多 tool 做意图路由；不平行第二套与 HicodeTaskQueue 无关的
「影子调度器」——目标层队列可以是权威任务图，叶子执行仍复用现有执行路径。
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("veya.goal_run")

# G2 调度停滞熔断: v0.1 叶子同步执行, 若连续这么多 tick 取不到任何可跑任务,
# 判定为无法推进 (陈旧 running_id / 依赖不可达), 退出循环交 G3 收敛为 blocked,
# 而不是空转自旋烧 CPU。
_MAX_STALL_TICKS = 3

from server.goal_run.leaf import execute_leaf_with_memory
from server.goal_run.models import (
    GoalRunResponse,
    GoalRunState,
    GoalStatus,
    TaskStatus,
)
from server.goal_run.planner import g1_plan
from server.goal_run.scheduler import (
    SchedulerDecision,
    SchedulerState,
    check_and_update_budget,
    pick_next_tasks,
)
from server.goal_run.store import (
    get_goal_run_artifacts,
    load_goal_run,
    read_events,
    save_goal_run,
    write_final_summary,
)
from server.goal_run.verify import apply_block_policy, verify_task


async def _constitution_guard(
    state: GoalRunState,
    task: Any,
    leaf_summary: str,
    project_root: str,
) -> GoalRunResponse | None:
    """Constitution is a red line: monitor intervene → stop the goal."""
    if not state.constitution:
        return None
    from omodul.execution_health_monitor import execution_health_monitor

    rec = await execution_health_monitor(
        {},
        {
            "tool_name": "leaf",
            "arguments": {"id": task.id},
            "execution_log": leaf_summary,
            "constitution": state.constitution,
        },
        Path(project_root) / ".veya-project" / "goal-runs" / state.goal_id,
    )
    action = ((rec or {}).get("findings") or {}).get("action") or "continue"
    if action != "intervene":
        return None
    task.status = TaskStatus.blocked
    task.block_reason = ((rec or {}).get("findings") or {}).get(
        "intervention_prompt"
    ) or "constitution_violation"
    return await apply_block_policy(state, task, "stop_goal")


def _record_retry_branch(
    task: Any, *, leaf_summary: str, verify_reason: str, new_instruction: str
) -> None:
    """验收失败重试: 用 SessionTreeMgr.branch() 开新叶, 不再是原地覆盖 instruction。

    对标"Pi"清单 P2 执行侧分支(见 memory project_veya_pi_gap_audit 步骤8)。复用
    步骤2 已落地的镜像树(`default_session_tree_mirror()`, 跟 chat_stream 共用同一
    个库/db), 不新开 db, 也不用曾经复活又废弃的旧 `~/.veya/loop/session_tree.db`
    路径。sid 用 `goalrun-<task.id>` 前缀跟聊天会话的 uuid4 sid 天然不撞命名空间。
    失败绝不拖垮任务调度——任何异常吞掉只记日志, 调用方仍会正常推进重试。
    """
    if os.environ.get("VEYA_GOAL_RUN_BRANCH_ENABLED", "1") == "0":
        return
    try:
        from veya.omodul.session_tree import default_session_tree_mirror

        tree = default_session_tree_mirror()
        if task.session_tree_sid is None:
            sid = f"goalrun-{task.id}"
            tree.ensure_session(sid, system=f"goal_run task {task.id}: {task.title}")
            task.session_tree_sid = sid
            task.session_tree_leaf = tree.leaf(sid)["id"]
        fail_node = tree.append(
            task.session_tree_sid,
            role="assistant",
            content=leaf_summary,
            parent_id=task.session_tree_leaf,
            meta={"verify_passed": False, "verify_reason": verify_reason, "retries": task.retries},
        )
        branch_node = tree.branch(
            task.session_tree_sid,
            at_node_id=fail_node,
            role="user",
            content=new_instruction,
            meta={"retry_attempt": task.retries},
        )
        task.session_tree_leaf = branch_node
    except Exception:
        logger.exception(
            "[goal_run %s] session_tree branch failed, skip (仅结构化追踪丢失, 不影响重试)", task.id
        )


async def _run_dual_axis_review(task: Any, project_root: str, before_ref: str) -> dict | None:
    """验收通过后跑一遍双轴审查(mattpocock/skills code-review 内化, 见 memory
    project_veya_pi_gap_audit)。advisory only——findings 只记录不拦任务完成
    (acceptance 验收才是真正准入门, LLM 审查判断是假设不是证据)。任何异常都
    静默跳过, 绝不拖垮任务调度。
    """
    if os.environ.get("VEYA_GOAL_RUN_CODE_REVIEW_ENABLED", "1") == "0":
        return None
    try:
        from server.goal_run.code_review import dual_axis_review
        from server.goal_run.git_diff import capture_task_diff

        diff_text = capture_task_diff(project_root, before_ref)
        if not diff_text.strip():
            return None
        standards_doc = ""
        claude_md = Path(project_root) / "CLAUDE.md"
        if claude_md.is_file():
            standards_doc = claude_md.read_text(encoding="utf-8")
        return await dual_axis_review(
            diff_text=diff_text,
            task_instruction=task.instruction,
            acceptance=task.acceptance,
            standards_doc=standards_doc,
        )
    except Exception:
        logger.exception(
            "[goal_run %s] dual-axis review failed, skip (advisory, 不影响任务完成)", task.id
        )
        return None


async def project_run_goal(
    project_root: str,
    goal: str,
    mode: str = "auto",
    resume_goal_id: str | None = None,
    parent_goal_clarification: str | None = None,
    max_wall_s: int | None = None,
    wait: bool = True,
) -> GoalRunResponse:
    """project_run_goal 主入口（M4 规格）。

    参数：
    - project_root: 项目根目录绝对路径
    - goal: 用户复杂目标文本
    - mode: auto(默认) | act_eager | ask_only
    - resume_goal_id: 若要 resume 一个未完成的 run，传该 goal_id
    - parent_goal_clarification: G0 追问的回答，用于续答链
    - max_wall_s: 可选，覆盖默认 max_wall_s 预算 (秒)
    - wait: True(默认) → 阻塞到终态或超时；False → 快速返回 running

    返回 GoalRunResponse（统一响应格式）。
    """
    start_ts = time.time()
    state: GoalRunState | None = None

    # ── R0: 数据模型 & store ──────────────────────────────────────────
    # 若 resume：加载已有 state
    if resume_goal_id:
        state = load_goal_run(project_root, resume_goal_id)
        if state is None:
            # 若目标不存在，则视为新 run
            state = None
        else:
            # 恢复模式：目标文本可能不同，保持原目标文本
            # (实际应用中可能需要目标文本冲突时的处理)
            pass

    # ── G0: Understand 门禁 ───────────────────────────────────────────
    from server.goal_run.planner import g0_understand

    memory = ""
    chain = None
    if state and state.tasks:
        # 若有现有 state，读取 memory (STATE/DECISIONS/LESSONS)
        from server.project_store import ProjectStore

        ps = ProjectStore(project_root)
        memory = ps.read_state()
        # chain: 从 events.jsonl 读取最近的 understand/plan 事件
        events = read_events(project_root, resume_goal_id)
        if events:
            # 简化处理：只取最近一次 understand 相关事件的 chain
            for ev in reversed(events):
                if ev.get("type") in ("understand_start", "plan_created"):
                    chain = ev.get("chain", [])
                    break

    u, g0_response = await g0_understand(
        goal_text=goal,
        memory=memory,
        chain=chain,
        mode=mode,
    )

    # 若 G0 返回 ask → 直接返回 understood_ask 响应
    if u.decision == "ask" and g0_response is not None:
        g0_response.next_action = "wait" if wait else "none"
        if not wait:
            # 不阻塞，直接返回 running 状态
            g0_response.status = GoalStatus.running
            g0_response.phase = "running"
            g0_response.next_action = "wait"
        return g0_response

    # 若 mode=ask_only 强制只追问
    if mode == "ask_only" and u.decision == "act":
        from server.goal_run.models import GoalRunResponse

        return GoalRunResponse(
            goal_id=g0_response.goal_id,
            status=GoalStatus.awaiting_user,
            phase="understood_ask",
            interpretation=u.interpretation,
            questions=u.questions,
            goal_counts=None,
            summary=None,
            block_reason="mode=ask_only: forced clarification",
            artifacts=None,
            next_action="wait",
        )

    # ── G1: Plan 任务图生成 ────────────────────────────────────────────
    if resume_goal_id and state and state.status == GoalStatus.planning:
        # 正在计划阶段，直接使用已有 state
        pass
    else:
        # 生成任务图
        if g0_response is None:
            # g0_response 已在上面构造，这里确保存在
            pass

        # 更新 budget (可选覆盖)
        budget = state.budget if state else {"max_wall_s": 7200, "max_leaf_tasks": 40}
        if max_wall_s is not None:
            budget["max_wall_s"] = max_wall_s

        state, _g1_response = await g1_plan(
            interpretation=u.interpretation or goal,
            assumptions=u.assumptions or [],
            goal_text=goal,
            default_assignee=state.default_assignee if state else "hicode",
            budget=budget,
            project_root=project_root,
        )

        # 保存 state
        save_goal_run(state, project_root)

    # 如果是 resume，保持原有状态
    if resume_goal_id and state:
        # 恢复已有的 taskgraph 状态
        pass

    # ── G2: Loop 调度 + 执行 + 验收 ───────────────────────────────────
    # 设置最大并发
    max_concurrent = 1  # v0.1 默认串行

    scheduler_state = SchedulerState(
        state=state or GoalRunState(goal_id="", goal_text=goal),
        max_concurrent=max_concurrent,
    )

    # 主循环
    state.status = GoalStatus.running
    save_goal_run(state, project_root)

    # 若是 ask_only，直接返回
    if mode == "ask_only":
        return (
            g0_response
            if g0_response
            else GoalRunResponse(
                goal_id=state.goal_id,
                status=GoalStatus.awaiting_user,
                phase="understood_ask",
                interpretation=u.interpretation,
                questions=u.questions,
                goal_counts=None,
                summary=None,
                block_reason="mode=ask_only",
                artifacts=None,
                next_action="wait",
            )
        )

    from obase.loop_breaker import init_breaker, reset_breaker

    breaker_token = init_breaker()
    try:
        return await _run_loop_and_finalize(
            state=state,
            goal=goal,
            u=u,
            project_root=project_root,
            start_ts=start_ts,
            scheduler_state=scheduler_state,
            max_concurrent=max_concurrent,
        )
    finally:
        reset_breaker(breaker_token)


async def _run_loop_and_finalize(
    *,
    state: GoalRunState,
    goal: str,
    u: Any,
    project_root: str,
    start_ts: float,
    scheduler_state: SchedulerState,
    max_concurrent: int,
) -> GoalRunResponse:
    # 主循环：tick 直到 terminal
    stall_ticks = 0  # 连续「无可跑任务」的 tick 计数 (熔断空转自旋)
    while not state.is_terminal():
        # 1. 检查预算
        elapsed_s = time.time() - start_ts
        budget_block = await check_and_update_budget(state, elapsed_s, scheduler_state)
        if budget_block is not None:
            return budget_block

        # 2. 调度：promote ready，pick next
        sched_decision = scheduler_state.promote_ready()
        if sched_decision == SchedulerDecision.all_done:
            # 所有任务完成，进入 G3
            break
        if sched_decision == SchedulerDecision.timeout:
            # 预算超时（已在 check_and_update_budget 中处理）
            break

        # 3. 选取任务进入 running
        running_ids = state.running_ids if state else set()
        batch = pick_next_tasks(state, max_concurrent, running_ids)
        if not batch:
            # 本 tick 没取到任何可跑任务。v0.1 叶子同步执行, tick 之间没有后台任务能
            # 改变状态 → 让出事件循环, 连续无进展即熔断: running_ids 已空则直接退出,
            # 否则最多等 _MAX_STALL_TICKS 个 tick 防陈旧 running_id 卡死, 杜绝忙等自旋。
            stall_ticks += 1
            await asyncio.sleep(0)
            if not state.running_ids or stall_ticks >= _MAX_STALL_TICKS:
                break
            continue
        stall_ticks = 0

        # 4. 执行每个 running 任务
        for task in batch:
            task.status = TaskStatus.running
            state.running_ids.add(task.id)
            state.completed_ids.discard(task.id)

            # 执行 leaf：复用 project_ask 内部路径
            from server.goal_run.git_diff import current_head

            before_ref = current_head(project_root)
            leaf_result = await execute_leaf_with_memory(
                project_root=project_root,
                instruction=task.instruction,
                acceptance=task.acceptance,
                assignee=task.assignee,
                constitution_text=state.constitution,
            )

            shield = await _constitution_guard(state, task, leaf_result.summary, project_root)
            if shield is not None:
                save_goal_run(state, project_root)
                return shield

            # 5. 验收
            task.status = TaskStatus.verifying
            verify_result = await verify_task(task, leaf_result.summary, project_root)

            # 叶子已同步执行完毕: 无论 pass/retry/block, 该任务都不再占用并发槽,
            # 必须从 running_ids 摘除。此前只 add 从不 discard → 陈旧 id 使 promote_ready
            # 永不返回 all_done、空转守卫永不 break, 是 resume 路径忙等自旋的根因。
            state.running_ids.discard(task.id)

            # 6. 根据验收结果更新状态
            if verify_result.passed:
                task.status = TaskStatus.completed
                task.execute_result = leaf_result.summary
                state.completed_ids.add(task.id)
                task.artifacts.extend(leaf_result.artifacts)
                task.review_findings = await _run_dual_axis_review(task, project_root, before_ref)
            else:
                # 验收失败：重试计数
                task.retries += 1
                if task.retries < state.budget.get("max_retries_per_task", 2):
                    # 重试：回到 ready 状态
                    task.status = TaskStatus.ready
                    # 可在 instruction 中记录上轮失败原因
                    new_instruction = (
                        f"{task.instruction}\n\n(上轮验收失败: {verify_result.reason})"
                    )
                    _record_retry_branch(
                        task,
                        leaf_summary=leaf_result.summary,
                        verify_reason=verify_result.reason,
                        new_instruction=new_instruction,
                    )
                    task.instruction = new_instruction
                else:
                    # 重试用尽：blocked
                    task.status = TaskStatus.blocked
                    task.block_reason = (
                        task.block_reason or f"verify failed after {task.retries} retries"
                    )
                    # 传播阻塞策略
                    apply_result = await apply_block_policy(state, task, "stop_goal")
                    if apply_result is not None:
                        return apply_result

        # 7. 保存 state（每 tick 保存一次）
        save_goal_run(state, project_root)

        # 8. 检查是否所有任务都完成或阻塞
        all_terminal = all(
            tn.status in (TaskStatus.completed, TaskStatus.blocked, TaskStatus.cancelled)
            for tn in state.tasks.values()
        )
        if all_terminal:
            break

    # ── G3: Finalize ───────────────────────────────────────────────────
    # 统计任务数
    total = len(state.tasks) if state else 0
    completed = (
        sum(1 for tn in state.tasks.values() if tn.status == TaskStatus.completed) if state else 0
    )
    blocked = (
        sum(1 for tn in state.tasks.values() if tn.status == TaskStatus.blocked) if state else 0
    )

    # 生成 final_summary
    summary_parts = []
    summary_parts.append(f"Goal: {state.goal_text if state else goal}")
    summary_parts.append(f"Status: {state.status.value if state else 'unknown'}")
    summary_parts.append(f"Tasks: {completed}/{total} completed, {blocked} blocked")
    if state and state.final_summary:
        summary_parts.append(state.final_summary)

    final_summary = "\n".join(summary_parts)

    # 写入 final_summary.md
    write_final_summary(
        project_root=project_root,
        goal_id=state.goal_id if state else "temp",
        summary=final_summary,
        artifacts=get_goal_run_artifacts(project_root, state.goal_id if state else "temp"),
    )

    # 更新 state 为 completed
    state.status = GoalStatus.completed
    state.final_summary = final_summary
    state.finished_at = datetime.now(timezone.utc)
    save_goal_run(state, project_root)

    # 返回最终响应
    return GoalRunResponse(
        goal_id=state.goal_id if state else "temp",
        status=GoalStatus.completed,
        phase="finalized",
        interpretation=u.interpretation if u else goal,
        questions=None,
        goal_counts={
            "pending": sum(1 for tn in state.tasks.values() if tn.status == TaskStatus.pending)
            if state
            else 0,
            "running": len(state.running_ids) if state else 0,
            "completed": completed,
            "blocked": blocked,
        },
        summary=final_summary,
        block_reason=None,
        artifacts=get_goal_run_artifacts(project_root, state.goal_id if state else "temp"),
        next_action="none",
    )
