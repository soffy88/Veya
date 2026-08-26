"""goal_run runner — project_run_goal 主流程。

实现 v0.1 Spec 的 durable execution 闭环：
显式 Objective → G1 Plan/compile → G2 Loop (调度 + 执行 + 验收) → G3 Finalize。

硬约束：Coordinator 不靠多 tool 做意图路由；GoalRun 不理解或重写用户意图；
不平行第二套与 HicodeTaskQueue 无关的「影子调度器」——目标层队列可以是权威
任务图，叶子执行仍复用现有执行路径。
"""

import asyncio
import contextlib
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.execution.adapters import delegate_result_from_leaf
from runtime.execution.artifacts import ArtifactStore
from runtime.execution.durable import (
    DurableExecutionError,
    DurableExecutionRepository,
    content_hash,
    new_id,
)
from runtime.execution.fanin import fan_in
from runtime.execution.finalization import FinalizationController
from runtime.execution.models import (
    DelegateRequest,
    DelegateResult,
    ExecutionCheckpoint,
    SpawnBudget,
)
from runtime.execution.no_progress import NoProgressGuard
from runtime.execution.spawn_guard import SpawnGuard
from server.capability_model import performance_store
from server.goal_run.leaf import execute_leaf_with_memory
from server.goal_run.models import (
    GoalRunResponse,
    GoalRunState,
    GoalStatus,
    TaskStatus,
)
from server.goal_run.planner import g1_plan
from server.goal_run.scheduler import SchedulerState
from server.goal_run.store import (
    append_event,
    get_goal_run_artifacts,
    load_goal_run,
    save_goal_run,
    write_final_summary,
)
from server.goal_run.trust_plane import (
    append_trust_plane_records,
    build_and_write_task_episode,
    record_task_verification,
)
from server.goal_run.verify import VerifyResult, apply_block_policy, verify_task
from server.memory_controller import memory_controller
from server.project_understand import UnderstandResult

logger = logging.getLogger("veya.goal_run")

# G2 调度停滞熔断: v0.1 叶子同步执行, 若连续这么多 tick 取不到任何可跑任务,
# 判定为无法推进 (陈旧 running_id / 依赖不可达), 退出循环交 G3 收敛为 blocked,
# 而不是空转自旋烧 CPU。
_MAX_STALL_TICKS = 3

# smart-ralph [P] marker 支持的并发上限(见 memory project_veya_pi_gap_audit)。
_MAX_PARALLEL_CONCURRENCY = 4
_VALID_GOAL_MODES = frozenset({"auto", "act_eager", "ask_only"})
_goal_cancel_events: dict[str, asyncio.Event] = {}


def _emit_runtime_event(
    state: GoalRunState,
    project_root: str,
    event_type: str,
    *,
    task_id: str | None = None,
    **payload: Any,
) -> None:
    """Project Execution Runtime facts to the existing SSE/event stores."""
    event = {"type": event_type, "goal_id": state.goal_id, **payload}
    if task_id is not None:
        event["task_id"] = task_id
    append_event(project_root, state.goal_id, event)
    from server.events import fire_step

    fire_step({"type": event_type, "goal_id": state.goal_id, "task_id": task_id, **payload})


def _explicit_understanding(goal: str, mode: str) -> UnderstandResult:
    """Treat the MasterAgent's goal as an already-authorized objective.

    This object preserves the old runner contract for downstream response
    formatting. It is not an intent classifier and never asks a second model
    to reinterpret the user's request.
    """
    return UnderstandResult(
        decision="ask" if mode == "ask_only" else "act",
        confidence=1.0,
        interpretation=str(goal).strip()[:500],
        assumptions=[],
        questions=[],
        risk_flags=[],
        reasons=["objective supplied by MasterAgent"],
    )


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


async def _run_plan_review_gate(
    state: Any, goal_text: str, project_root: str
) -> GoalRunResponse | None:
    """G1 出图后的计划前置双轴审查(oh-my-openagent orchestration 内化, 见 memory
    project_veya_pi_gap_audit)。真正的门禁, 不是 advisory——Feasibility/Safety
    任一轴明确 reject 就拦, 状态停在 awaiting_user 不进 G2。LLM 失败/关闭开关
    都放行(fail open), 只有拿到明确 reject 才拦。
    """
    if os.environ.get("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "1") == "0":
        state.plan_review = {"skipped": True}
        return None
    try:
        from server.goal_run.plan_review import dual_axis_plan_review

        tasks = [
            {
                "id": t.id,
                "title": t.title,
                "instruction": t.instruction,
                "acceptance": t.acceptance,
                "depends_on": t.depends_on,
            }
            for t in state.tasks.values()
        ]
        report = await dual_axis_plan_review(goal_text=goal_text, tasks=tasks)
    except Exception:
        logger.exception("[goal_run %s] plan review failed, fail open (放行)", state.goal_id)
        state.plan_review = {"error": "plan review raised, failed open"}
        save_goal_run(state, project_root)
        return None

    state.plan_review = report
    if not report.get("blocked"):
        save_goal_run(state, project_root)
        return None

    state.status = GoalStatus.awaiting_user
    save_goal_run(state, project_root)
    concerns = list(report["feasibility"].get("concerns") or []) + list(
        report["safety"].get("concerns") or []
    )
    return GoalRunResponse(
        goal_id=state.goal_id,
        status=GoalStatus.awaiting_user,
        phase="plan_review_blocked",
        interpretation=None,
        questions=None,
        goal_counts=state.snapshot_running(),
        summary=None,
        block_reason="; ".join(concerns) or "plan review rejected",
        artifacts=None,
        next_action="wait",
    )


def _record_trust_plane(
    task: Any,
    state: Any,
    project_root: str,
    *,
    verify_result: Any,
    diff_text: str,
    review_findings: dict | None,
) -> bool:
    """把本次验收结果按 VAOM Trust Plane schema 旁路记一份(见 docs/dev/rfc-01-vaom.md,
    server/goal_run/trust_plane.py)。纯旁路——task.status 的转移逻辑不依赖这个函数的
    返回值, 除非 VEYA_GOAL_RUN_VERIFIED_GATE=1(见 _process_one_task)。任何异常都吞掉
    只记日志, 记录失败不该拖垮任务调度本身(除非显式开了 gate)。

    返回是否成功写出了一条 VerifiedState(供 gate 判断用; verify 没过时天然是 False)。
    """
    if os.environ.get("VEYA_GOAL_RUN_TRUST_PLANE_ENABLED", "1") == "0":
        return True  # 记录整体关闭时不产生任何 gate 副作用
    try:
        claim, evidences, evaluations, verified_state = record_task_verification(
            task_id=task.id,
            goal_id=state.goal_id,
            actor=task.assignee,
            statement=verify_result.summary,
            target_refs=task.acceptance,
            verify_passed=verify_result.passed,
            verify_summary=verify_result.summary,
            diff_text=diff_text,
            review_findings=review_findings,
        )
        append_trust_plane_records(
            project_root,
            state.goal_id,
            claim=claim,
            evidences=evidences,
            evaluations=evaluations,
            verified_state=verified_state,
        )
        return verified_state is not None
    except Exception:
        logger.exception(
            "[goal_run %s] trust_plane record failed for task %s", state.goal_id, task.id
        )
        return False


def _record_performance_sample(task: Any, *, success: bool) -> None:
    """goal_run 每次任务验收结果顺带喂一条样本给 PerformanceStore(VAOM
    PerformanceProfile, 见 server/capability_model.py, docs/dev/rfc-01-vaom.md)。
    task_archetype 目前只有一个粗粒度桶("goal_run_task")——按任务标题/instruction
    做更细的归类是后续需要时再加, 这里先诚实反映"暂时没有归类能力"而不是编一个
    看起来更细但其实是瞎猜的 archetype。纯旁路, 异常吞掉不拖垮任务调度。
    """
    try:
        performance_store.record_outcome(
            harness_id=task.assignee,
            task_archetype="goal_run_task",
            success=success,
        )
    except Exception:
        logger.exception(
            "[goal_run] performance sample record failed for task %s (harness=%s)",
            task.id,
            task.assignee,
        )


async def _process_one_task(task: Any, state: Any, project_root: str) -> GoalRunResponse | None:
    """单个任务的执行→验收→重试/完成/双轴审查全流程。抽成独立协程是为了让
    [P] 批次能用 asyncio.gather 并发跑(见 memory project_veya_pi_gap_audit
    smart-ralph 内化)——不是把这段逻辑重写了一遍, 只是从内联 for 循环体挪出来。

    返回非 None = 整个 goal 要终止(constitution 违规 / 重试用尽触发的阻塞策略),
    调用方负责 save_goal_run 后把这个响应原样返回给上层。
    """
    task.status = TaskStatus.running
    state.running_ids.add(task.id)
    state.completed_ids.discard(task.id)

    from server.goal_run.git_diff import current_head

    before_ref = current_head(project_root)
    try:
        leaf_result = await execute_leaf_with_memory(
            project_root=project_root,
            instruction=task.instruction,
            acceptance=task.acceptance,
            assignee=task.assignee,
            constitution_text=state.constitution,
        )
    except asyncio.CancelledError:
        task.status = TaskStatus.cancelled
        task.stop_reason = "cancelled"
        task.unfinished_work.append(task.instruction)
        state.running_ids.discard(task.id)
        raise
    except Exception as exc:
        # A leaf crash is data for finalization, not permission to discard all
        # sibling work.  Preserve the task as a failed delegate and let the
        # caller decide whether this is a safety stop or a partial completion.
        from server.goal_run.leaf import LeafResult

        leaf_result = LeafResult(
            status="blocked",
            summary="",
            block_reason=f"{type(exc).__name__}: {exc}",
            stop_reason="exception",
            unfinished_work=[task.instruction],
        )

    task.stop_reason = leaf_result.stop_reason or (
        "completed" if leaf_result.status == "completed" else "exception"
    )
    task.execute_result = leaf_result.summary
    task.artifacts.extend(
        [
            str(
                getattr(item, "path", None)
                or (item.get("path") if isinstance(item, dict) else item)
            )
            for item in (leaf_result.artifacts or [])
        ]
    )
    for item in leaf_result.evidence or []:
        task.evidence.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    for item in leaf_result.assertions or []:
        task.assertions.append(item.to_dict() if hasattr(item, "to_dict") else dict(item))
    task.unfinished_work.extend(leaf_result.unfinished_work or [])
    delegate_request = DelegateRequest(
        delegate_id=task.id,
        parent_task_id=state.goal_id,
        parent_trace_id=state.goal_id,
        objective=task.instruction,
        acceptance=task.acceptance,
        depth=1,
        estimated_tokens=0,
        timeout_s=int(state.budget.get("subagent_timeout_s", 5400)),
        workspace=project_root,
        output_paths=list(task.artifacts),
    )
    task.delegate_result = delegate_result_from_leaf(delegate_request, leaf_result).to_dict()

    shield = await _constitution_guard(state, task, leaf_result.summary, project_root)
    if shield is not None:
        state.running_ids.discard(task.id)
        task.stop_reason = "permission_denied"
        return shield

    # 5. 验收
    task.status = TaskStatus.verifying
    if leaf_result.status != "completed":
        verify_result = VerifyResult(
            passed=False,
            summary=leaf_result.block_reason or "delegate did not complete",
            reason=leaf_result.block_reason or "delegate did not complete",
        )
    else:
        verify_result = await verify_task(task, leaf_result.summary, project_root)

    # 叶子已同步执行完毕: 无论 pass/retry/block, 该任务都不再占用并发槽,
    # 必须从 running_ids 摘除。此前只 add 从不 discard → 陈旧 id 使 promote_ready
    # 永不返回 all_done、空转守卫永不 break, 是 resume 路径忙等自旋的根因。
    state.running_ids.discard(task.id)

    # 6. 根据验收结果更新状态
    if verify_result.passed:
        task.status = TaskStatus.completed
        task.stop_reason = "completed"
        state.completed_ids.add(task.id)
        task.review_findings = await _run_dual_axis_review(task, project_root, before_ref)

        from server.goal_run.git_diff import capture_task_diff

        verified_ok = _record_trust_plane(
            task,
            state,
            project_root,
            verify_result=verify_result,
            diff_text=capture_task_diff(project_root, before_ref),
            review_findings=task.review_findings,
        )
        _record_performance_sample(task, success=True)
        # GoalKernel Verified Gate(PR-09, 见 docs/dev/rfc-01-vaom.md/VEYA_3.0_GAP_AUDIT.md §3.4):
        # 默认关闭——verify_result.passed 本来就已经是"独立验收通过"而不是 worker 自报
        # (task.execute_result 从不直接驱动 status), 这个 gate 加的是更严格的一层:
        # 连 VerifiedState 记录本身都必须落盘成功, 任务才算真正 completed。只有显式
        # 开启时才会因为记录失败把已经验收通过的任务打回 blocked, 因此默认关闭,
        # 不属于"纯文档/测试"改动, 打开前需按 ARCHITECTURE_STABLE.md §4 先获用户同意。
        if os.environ.get("VEYA_GOAL_RUN_VERIFIED_GATE", "0") == "1" and not verified_ok:
            task.status = TaskStatus.blocked
            state.completed_ids.discard(task.id)
            task.block_reason = "verified_state_write_failed (VEYA_GOAL_RUN_VERIFIED_GATE=1)"
            return await apply_block_policy(state, task, "stop_goal")
        return None

    # 验收失败：重试计数
    task.retries += 1
    if task.retries < state.budget.get("max_retries_per_task", 2):
        # 重试：回到 ready 状态
        task.status = TaskStatus.ready
        # 可在 instruction 中记录上轮失败原因
        new_instruction = f"{task.instruction}\n\n(上轮验收失败: {verify_result.reason})"
        _record_retry_branch(
            task,
            leaf_summary=leaf_result.summary,
            verify_reason=verify_result.reason,
            new_instruction=new_instruction,
        )
        task.instruction = new_instruction
        return None

    # 重试用尽：blocked
    if task.delegate_result:
        task.delegate_result["status"] = "partial"
        task.delegate_result["stop_reason"] = "acceptance_failed"
    task.status = TaskStatus.blocked
    task.stop_reason = "acceptance_failed"
    task.block_reason = task.block_reason or f"verify failed after {task.retries} retries"

    from server.goal_run.git_diff import capture_task_diff

    _record_trust_plane(
        task,
        state,
        project_root,
        verify_result=verify_result,
        diff_text=capture_task_diff(project_root, before_ref),
        review_findings=None,
    )
    _record_performance_sample(task, success=False)
    return await apply_block_policy(state, task, "stop_goal")


def _take_continuous_tasks(
    state: GoalRunState,
    max_concurrent: int,
    active: dict[str, asyncio.Task[Any]],
) -> list[Any]:
    """Take the next safe ready tasks without introducing a batch barrier.

    A running parallel group can be filled as soon as a slot opens.  A
    non-parallel task remains exclusive and is never skipped to let a later
    task jump the queue.
    """
    ready = sorted(
        (task for task in state.tasks.values() if task.status == TaskStatus.ready),
        key=lambda task: task.id,
    )
    if not ready or len(active) >= max_concurrent:
        return []
    if active and (
        any(not state.tasks[task_id].parallel for task_id in active) or not ready[0].parallel
    ):
        return []

    available = max_concurrent - len(active)
    selected: list[Any] = []
    for task in ready:
        if task.id in active or not task.parallel:
            break
        selected.append(task)
        if len(selected) >= available:
            break
    if not active and ready and not ready[0].parallel:
        selected = [ready[0]]

    for task in selected:
        task.status = TaskStatus.running
        state.running_ids.add(task.id)
    return selected


def _mark_unfinished(state: GoalRunState) -> None:
    """Record work that finalization could not execute or verify."""
    for task in state.tasks.values():
        if (
            task.status
            in (
                TaskStatus.pending,
                TaskStatus.ready,
                TaskStatus.running,
                TaskStatus.verifying,
                TaskStatus.blocked,
                TaskStatus.cancelled,
            )
            and task.id not in state.completed_ids
        ):
            if task.id not in state.unfinished_work:
                state.unfinished_work.append(task.id)
            if task.instruction not in task.unfinished_work:
                task.unfinished_work.append(task.instruction)


def _write_execution_checkpoint(
    state: GoalRunState,
    project_root: str,
    active_ids: list[str],
    artifact_manifest_ref: str | None = None,
) -> None:
    """Persist a restart-safe scheduler snapshot beside the GoalRun state."""
    from runtime.execution.checkpoint import ExecutionCheckpointStore

    checkpoint = ExecutionCheckpoint(
        event_cursor=datetime.now(UTC).isoformat(),
        scheduler_snapshot={
            "goal_id": state.goal_id,
            "status": state.status.value,
            "running_ids": list(state.running_ids),
        },
        running_delegate_ids=list(active_ids),
        completed_task_ids=sorted(state.completed_ids),
        pending_task_ids=sorted(
            task.id
            for task in state.tasks.values()
            if task.status in (TaskStatus.pending, TaskStatus.ready)
        ),
        artifact_manifest_ref=artifact_manifest_ref,
        finalization_started=state.finalization_started,
    )
    state.runtime_checkpoint = checkpoint.to_dict()
    store = ExecutionCheckpointStore(Path(project_root) / ".veya" / "runs" / state.goal_id)
    store.write(checkpoint)


async def _prepare_durable_goal(
    state: GoalRunState,
) -> tuple[DurableExecutionRepository, str] | None:
    """Write-through the GoalRun plan and create one process-incarnation worker.

    The existing file projection remains the compatibility read model while
    the durable feature flag is off.  Once enabled, every leaf is also a
    durable logical work item and the worker must claim that item before
    executing it.
    """
    from runtime.execution.runtime import get_durable_runtime

    runtime = get_durable_runtime()
    if not runtime.config.enabled or not runtime.config.queue_claim:
        return None
    if not runtime._started:
        await runtime.start()
    repository = runtime.repository
    await repository.create_goal_run(
        goal_run_id=state.goal_id,
        root_run_id=state.goal_id,
        master_agent_id="master",
        status="running",
        budget=state.budget,
        acceptance=[],
        idempotency_key=f"goal-run:{state.goal_id}",
    )
    for task in state.tasks.values():
        await repository.enqueue_work_item(
            {
                "goal_run_id": state.goal_id,
                "logical_key": task.id,
                "kind": "goal_leaf",
                "payload": {
                    "instruction": task.instruction,
                    "assignee": task.assignee,
                    "acceptance": task.acceptance,
                },
                "depends_on": task.depends_on,
                "parallel": task.parallel,
                "side_effect_policy": "manual_on_unknown"
                if task.assignee in {"hicode", "dsh"}
                else "none",
                "max_attempts": int(state.budget.get("max_retries_per_task", 2)) + 1,
            },
            idempotency_key=f"{state.goal_id}:leaf:{task.id}",
        )
    # A restarted runner must rebuild its local projection from durable child
    # outcomes before the scheduler makes another decision.  In particular,
    # a task that was locally ``running`` when the process died must become
    # ready again after the reconciler classified its expired attempt; a
    # committed child must never be executed a second time.
    durable_items = {
        row["logical_key"]: row for row in await repository.list_work_items(state.goal_id)
    }
    for task in state.tasks.values():
        row = durable_items.get(task.id)
        if row is None:
            continue
        durable_state = row["state"]
        if durable_state == "succeeded":
            result = row.get("result_json")
            if isinstance(result, str):
                import json

                with contextlib.suppress(json.JSONDecodeError):
                    result = json.loads(result)
            if isinstance(result, dict) and isinstance(result.get("delegate_result"), dict):
                task.delegate_result = result["delegate_result"]
                task.execute_result = str(
                    result["delegate_result"].get("summary") or task.execute_result or ""
                )
            task.status = TaskStatus.completed
            state.completed_ids.add(task.id)
            state.running_ids.discard(task.id)
        elif durable_state in {"ready", "retry_wait"}:
            task.status = TaskStatus.ready
            state.running_ids.discard(task.id)
        elif durable_state == "created":
            # Durable ``created`` means dependencies are not yet satisfied;
            # keep the local projection pending so it cannot bypass the
            # repository's dependency check and be falsely marked blocked.
            task.status = TaskStatus.pending
            state.running_ids.discard(task.id)
        elif durable_state == "cancelled":
            task.status = TaskStatus.cancelled
            task.stop_reason = "cancelled"
            state.running_ids.discard(task.id)
        elif durable_state in {"failed", "quarantined_unknown", "unknown"}:
            task.status = TaskStatus.blocked
            task.stop_reason = (
                "manual_review"
                if durable_state in {"quarantined_unknown", "unknown"}
                else "exception"
            )
            task.block_reason = str(row.get("error_json") or durable_state)
            state.running_ids.discard(task.id)
            if task.id not in state.unfinished_work:
                state.unfinished_work.append(task.id)
    worker_id = f"goalrun/{os.uname().nodename}/{os.getpid()}/{new_id()}"
    await repository.register_worker(worker_id=worker_id, incarnation_id=worker_id)
    return repository, worker_id


async def project_run_goal(
    project_root: str,
    goal: str,
    tasks: list[dict[str, Any]] | None = None,
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
    if mode not in _VALID_GOAL_MODES:
        return GoalRunResponse(
            goal_id="",
            status=GoalStatus.blocked,
            phase="rejected",
            interpretation=None,
            questions=None,
            goal_counts=None,
            summary=None,
            block_reason=f"unknown mode {mode!r}, must be one of {sorted(_VALID_GOAL_MODES)}",
            artifacts=None,
            next_action="none",
        )

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

    # ── Objective boundary ─────────────────────────────────────────────
    # A GoalRun receives a semantic decision from MasterAgent. It persists
    # and executes that decision; it does not perform G0 intent understanding.
    u = _explicit_understanding(goal, mode)
    g0_response = GoalRunResponse(
        goal_id=(state.goal_id if state else "temp_" + goal[:20]),
        status=GoalStatus.awaiting_user if mode == "ask_only" else GoalStatus.running,
        phase="awaiting_user" if mode == "ask_only" else "planning",
        interpretation=u.interpretation,
        questions=None,
        goal_counts=None,
        summary=None,
        block_reason="mode=ask_only" if mode == "ask_only" else None,
        artifacts=None,
        next_action="wait" if mode == "ask_only" else "plan",
    )
    if mode == "ask_only":
        return g0_response

    # A resume of a terminal run is a read of the durable result, not a new
    # semantic plan or a duplicate side-effectful execution.
    if resume_goal_id and state is not None and state.is_terminal():
        return GoalRunResponse(
            goal_id=state.goal_id,
            status=state.status,
            phase="finalized" if state.status != GoalStatus.blocked else "blocked",
            interpretation=u.interpretation,
            questions=None,
            goal_counts=state.snapshot_running(),
            summary=state.final_summary,
            block_reason=state.last_stop_reason,
            artifacts=get_goal_run_artifacts(project_root, state.goal_id),
            next_action="none",
        )

    # ── G1: Plan 任务图生成 ────────────────────────────────────────────
    if resume_goal_id and state is not None:
        # 恢复已有任务图；不得重新规划或重复创建任务。
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
            explicit_tasks=tasks,
        )

        if state.started_at is None:
            state.started_at = datetime.now(UTC)
        # 保存 state
        save_goal_run(state, project_root)

    # 如果是 resume，保持原有状态
    if resume_goal_id and state:
        # 恢复已有的 taskgraph 状态
        pass

    # ── 计划前置双轴审查(oh-my-openagent orchestration 内化, 见 memory
    # project_veya_pi_gap_audit): G1 出图后、G2 执行前跑一遍, 拦下还没烧执行
    # 预算, 比事后审更值。plan_review is None 才跑(resume 场景不重复审)。
    if state.plan_review is None:
        blocked_response = await _run_plan_review_gate(state, goal, project_root)
        if blocked_response is not None:
            return blocked_response

    # ── G2: Loop 调度 + 执行 + 验收 ───────────────────────────────────
    # 设置最大并发: smart-ralph [P] marker 支持(见 memory
    # project_veya_pi_gap_audit)。只有 tasks.md 里显式标 [P] 的任务才会被
    # 并发调度, 没标的严格串行——parallel_markers.py 已经把这个声明写进
    # TaskNode.parallel 了, 这里只按数量算并发上限, 不重新判断"要不要并行"。
    # 上限封顶(_MAX_PARALLEL_CONCURRENCY): 两个并行任务如果共享同一个
    # project_root, 底层 hicode CLI 快照/提交仍会被 SandboxBroker 的
    # workspace 锁序列化(见 server/hicode_agent.py) ——"并行"更多是缩短
    # LLM 思考/工具调用的等待重叠, 不是无限制的真并发, 封顶避免无意义地
    # 挤占资源。
    max_concurrent = 1
    parallel_count = sum(1 for tn in state.tasks.values() if tn.parallel) if state else 0
    if parallel_count > 0:
        max_concurrent = min(parallel_count, _MAX_PARALLEL_CONCURRENCY)
        logger.info(
            "[goal_run %s] detected %d [P]-marked tasks, max_concurrent=%d",
            state.goal_id,
            parallel_count,
            max_concurrent,
        )

    scheduler_state = SchedulerState(
        state=state or GoalRunState(goal_id="", goal_text=goal),
        max_concurrent=max_concurrent,
    )
    if state.started_at is None:
        state.started_at = datetime.now(UTC)
    cancel_event = _goal_cancel_events.setdefault(state.goal_id, asyncio.Event())

    durable_context = await _prepare_durable_goal(state) if state is not None else None

    # 主循环
    state.status = GoalStatus.finalizing if state.finalization_started else GoalStatus.running
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
            cancel_event=cancel_event,
            durable_repository=durable_context[0] if durable_context else None,
            durable_worker_id=durable_context[1] if durable_context else None,
        )
    finally:
        reset_breaker(breaker_token)
        _goal_cancel_events.pop(state.goal_id, None)


async def cancel_goal(project_root: str, goal_id: str) -> GoalRunResponse:
    """Request cancellation; a live runner will enter finalization first."""
    state = load_goal_run(project_root, goal_id)
    if state is None:
        return GoalRunResponse(
            goal_id=goal_id,
            status=GoalStatus.blocked,
            phase="rejected",
            block_reason=f"goal run {goal_id} not found",
            next_action="none",
        )
    if state.is_terminal():
        return GoalRunResponse(
            goal_id=goal_id,
            status=state.status,
            phase="finalized",
            summary=state.final_summary,
            artifacts=get_goal_run_artifacts(project_root, goal_id),
            next_action="none",
        )
    durable_cancel = None
    from runtime.execution.runtime import get_durable_runtime

    durable_runtime = get_durable_runtime()
    if durable_runtime.config.enabled:
        if not durable_runtime._started:
            await durable_runtime.start()
        durable_cancel = await durable_runtime.repository.request_cancel(goal_id, actor="user")
    event = _goal_cancel_events.get(goal_id)
    if event is not None:
        event.set()
        requested_status = (durable_cancel or {}).get("status")
        response_status = GoalStatus.finalizing
        if requested_status in {item.value for item in GoalStatus}:
            response_status = GoalStatus(requested_status)
        return GoalRunResponse(
            goal_id=goal_id,
            status=response_status,
            phase="finalizing",
            summary=None,
            artifacts=get_goal_run_artifacts(project_root, goal_id),
            next_action="wait",
        )
    state.status = (
        GoalStatus.partial_completed
        if any(task.artifacts or task.evidence for task in state.tasks.values())
        else GoalStatus.cancelled
    )
    state.finalization_started = True
    state.last_stop_reason = "cancelled"
    _mark_unfinished(state)
    save_goal_run(state, project_root)
    return GoalRunResponse(
        goal_id=goal_id,
        status=state.status,
        phase="finalized",
        summary=state.final_summary,
        artifacts=get_goal_run_artifacts(project_root, goal_id),
        next_action="none",
    )


def _finalize_episode(state: Any, project_root: str, *, outcome: str) -> None:
    """goal 到达终态(正常完成或提前终止)时聚合一份 TaskEpisode(见 trust_plane.py),
    并从中提炼候选记忆(VAOM MemoryController.extract_candidates, 见
    server/memory_controller.py, docs/dev/rfc-01-vaom.md P3)。异常照例吞掉——
    这是旁路记录, 不该在 goal 已经算完的最后一步反而拖垮返回。
    """
    try:
        build_and_write_task_episode(
            project_root,
            state.goal_id,
            state.goal_text,
            task_ids=list(state.tasks.keys()),
            outcome=outcome,
            started_at=state.started_at.isoformat() if state.started_at else None,
            completed_at=datetime.now(UTC).isoformat(),
        )
        # The old JSON MemoryController remains available to isolated 0.9
        # tests, but production GoalRun candidate extraction must use the same
        # durable Personal Runtime authority as the rest of the application.
        if os.environ.get("VEYA_EXECUTION_DATABASE_URL"):
            from runtime.personal import get_personal_runtime
            from server.goal_run.trust_plane import read_task_episode, read_trust_plane_records

            async def _persist_candidates() -> None:
                episode = read_task_episode(project_root, state.goal_id)
                if episode is None:
                    return
                records = read_trust_plane_records(project_root, state.goal_id)
                claims = {item["claim_id"]: item for item in records if item["_type"] == "Claim"}
                source = await get_personal_runtime().record_event(
                    "memory.goal_run_candidate_source",
                    {"goal_id": state.goal_id, "outcome": outcome},
                    task_id=state.goal_id,
                    workspace_id=str(project_root),
                )
                for verified in (item for item in records if item["_type"] == "VerifiedState"):
                    claim = claims.get(verified["claim_id"])
                    if claim is None:
                        continue
                    await get_personal_runtime().create_memory_candidate(
                        claim["statement"],
                        scope_type="workspace",
                        scope_id=str(project_root),
                        memory_type="episodic",
                        source_event_ids=[source["id"]],
                        source_task_ids=[state.goal_id],
                        confidence=0.8,
                        reason="GoalRun verified state; pending promotion",
                        provenance={"goal_id": state.goal_id, "episode_id": episode["episode_id"]},
                        trace_id=state.goal_id,
                    )

            try:
                asyncio.get_running_loop().create_task(_persist_candidates())
            except RuntimeError:
                asyncio.run(_persist_candidates())
        else:
            memory_controller.extract_candidates(project_root, state.goal_id)
    except Exception:
        logger.exception("[goal_run %s] task episode finalize failed", state.goal_id)


async def _run_loop_and_finalize(
    *,
    state: GoalRunState,
    goal: str,
    u: Any,
    project_root: str,
    start_ts: float,
    scheduler_state: SchedulerState,
    max_concurrent: int,
    cancel_event: asyncio.Event | None = None,
    durable_repository: DurableExecutionRepository | None = None,
    durable_worker_id: str | None = None,
) -> GoalRunResponse:
    total_wall_s = float(state.budget.get("max_wall_s", 7200))
    finalization = FinalizationController(
        total_wall_s,
        min_reserve_s=float(state.budget.get("finalization_min_reserve_s", 180)),
        reserve_ratio=float(state.budget.get("finalization_reserve_ratio", 0.15)),
        max_reserve_s=float(state.budget.get("finalization_max_reserve_s", 900)),
    )

    def _spawn_event(event: dict[str, Any]) -> None:
        _emit_runtime_event(
            state,
            project_root,
            str(event.get("type") or "scheduler.updated"),
            task_id=str(event.get("job_id")) if event.get("job_id") else None,
        )

    spawn_guard = SpawnGuard(
        SpawnBudget(
            max_parallel=max_concurrent,
            max_tokens=int(state.budget.get("max_tokens", 300_000)),
            max_cost_usd=(
                float(state.budget["max_cost_usd"])
                if state.budget.get("max_cost_usd") is not None
                else None
            ),
            root_wall_time_s=int(total_wall_s),
            subagent_timeout_s=int(state.budget.get("subagent_timeout_s", 5400)),
        ),
        on_event=_spawn_event,
    )
    state.finalization_reserve_s = finalization.reserve_s
    if state.finalization_started:
        finalization.started = True
        state.status = GoalStatus.finalizing

    # Active tasks are independent asyncio tasks.  We wait for FIRST_COMPLETED
    # and immediately fill the freed slot, so a slow sibling cannot create a
    # hidden batch barrier.
    active: dict[str, asyncio.Task[Any]] = {}
    goal_no_progress = NoProgressGuard(
        "goal",
        threshold=int(state.budget.get("no_progress_goal_ticks", _MAX_STALL_TICKS)),
    )
    safety_response: GoalRunResponse | None = None
    _write_execution_checkpoint(state, project_root, [])

    async def cancel_active() -> None:
        for child in active.values():
            if not child.done():
                child.cancel()
        if active:
            await asyncio.gather(*active.values(), return_exceptions=True)

    while True:
        elapsed_s = max(0.0, time.time() - start_ts)
        remaining_s = max(0.0, total_wall_s - elapsed_s)

        operator_stop = bool(cancel_event and cancel_event.is_set())
        spawn_snapshot = spawn_guard.snapshot()
        budget_near = spawn_snapshot["used_tokens"] + spawn_snapshot[
            "reserved_tokens"
        ] >= spawn_snapshot["max_tokens"] or (
            spawn_guard.budget.max_cost_usd is not None
            and spawn_snapshot["used_cost_usd"] + spawn_snapshot["reserved_cost_usd"]
            >= spawn_guard.budget.max_cost_usd
        )
        if not finalization.started and finalization.start(
            remaining_s,
            budget_near=budget_near,
            operator_stop=operator_stop,
        ):
            if durable_repository is not None:
                # Persist the finalizer edge before exposing the local SSE
                # transition.  This is the durable-before-visible boundary:
                # a crash during the reserve must leave resumable work.
                await durable_repository.ensure_finalization_item(state.goal_id)
            state.finalization_started = True
            state.status = GoalStatus.finalizing
            _emit_runtime_event(
                state,
                project_root,
                "finalization.started",
                reason=(
                    "operator_stop"
                    if operator_stop
                    else "budget_near"
                    if budget_near
                    else "wall_reserve"
                ),
                remaining_wall_s=remaining_s,
                reserve_s=finalization.reserve_s,
            )
            _write_execution_checkpoint(state, project_root, list(active))
            save_goal_run(state, project_root)

        if finalization.started and (remaining_s <= 0 or operator_stop) and active:
            await cancel_active()

        if not finalization.started:
            scheduler_state.promote_ready()
            selected = _take_continuous_tasks(state, max_concurrent, active)
            for task in selected:
                _emit_runtime_event(state, project_root, "scheduler.task_ready", task_id=task.id)
                _emit_runtime_event(state, project_root, "scheduler.task_started", task_id=task.id)

                async def _run_guarded(current_task: Any = task):
                    async def execute_current(_cancel: asyncio.Event):
                        if durable_repository is None or durable_worker_id is None:
                            return await _process_one_task(current_task, state, project_root)
                        claim = await durable_repository.claim_next(
                            durable_worker_id,
                            capabilities={"*"},
                            kinds={"goal_leaf"},
                            goal_run_id=state.goal_id,
                            logical_key=current_task.id,
                            lease_ttl_s=30,
                        )
                        if claim is None:
                            raise DurableExecutionError(
                                "NOT_FOUND", f"no durable claim for {current_task.id}"
                            )
                        await durable_repository.start(claim)
                        try:
                            response = await _process_one_task(current_task, state, project_root)
                            if current_task.status == TaskStatus.completed:
                                await durable_repository.complete(
                                    claim,
                                    {
                                        "task_id": current_task.id,
                                        "status": "completed",
                                        "summary": current_task.execute_result or "",
                                        "delegate_result": current_task.delegate_result,
                                    },
                                )
                            elif current_task.status == TaskStatus.ready:
                                await durable_repository.fail(
                                    claim,
                                    {"message": "acceptance failed; retry branch remains ready"},
                                    classification="safe_retry",
                                )
                            else:
                                await durable_repository.fail(
                                    claim,
                                    {
                                        "message": current_task.block_reason
                                        or "leaf did not complete"
                                    },
                                    classification="permanent_failure",
                                )
                            return response
                        except asyncio.CancelledError:
                            with contextlib.suppress(DurableExecutionError):
                                await durable_repository.fail(
                                    claim,
                                    {"message": "worker cancelled"},
                                    classification="cancelled",
                                )
                            raise
                        except DurableExecutionError:
                            raise
                        except Exception as exc:
                            classification = (
                                "unknown"
                                if current_task.assignee in {"hicode", "dsh"}
                                else "safe_retry"
                            )
                            with contextlib.suppress(DurableExecutionError):
                                await durable_repository.fail(
                                    claim,
                                    {"message": f"{type(exc).__name__}: {exc}"},
                                    classification=classification,
                                )
                            raise

                    return await spawn_guard.run(
                        current_task.id,
                        execute_current,
                        depth=1,
                        estimated_tokens=0,
                        timeout_s=int(state.budget.get("subagent_timeout_s", 5400)),
                    )

                active[task.id] = asyncio.create_task(
                    _run_guarded(),
                    name=f"veya-goal-{state.goal_id}-{task.id}",
                )
            if selected:
                goal_no_progress.reset()

        if active:
            # Re-check the reserve periodically even when the current child is
            # slow.  Once the root deadline is reached the children are
            # cancelled and their partial projection is finalized below.
            wait_timeout = min(1.0, max(0.05, remaining_s))
            done, _ = await asyncio.wait(
                list(active.values()), timeout=wait_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                continue
            for child in done:
                task_id = next(key for key, value in list(active.items()) if value is child)
                active.pop(task_id, None)
                task = state.tasks[task_id]
                try:
                    response = child.result()
                except asyncio.CancelledError:
                    task.status = TaskStatus.cancelled
                    task.stop_reason = "cancelled"
                    state.running_ids.discard(task_id)
                    if task_id not in state.unfinished_work:
                        state.unfinished_work.append(task_id)
                    _emit_runtime_event(state, project_root, "delegate.cancelled", task_id=task_id)
                    response = None
                except Exception as exc:
                    task.status = TaskStatus.blocked
                    task.stop_reason = "exception"
                    task.block_reason = f"{type(exc).__name__}: {exc}"
                    task.unfinished_work.append(task.instruction)
                    state.running_ids.discard(task_id)
                    _emit_runtime_event(
                        state,
                        project_root,
                        "delegate.failed",
                        task_id=task_id,
                        error=str(exc),
                    )
                    response = None
                else:
                    if response is None and task.status == TaskStatus.completed:
                        _emit_runtime_event(
                            state, project_root, "delegate.completed", task_id=task_id
                        )
                    elif response is None and task.status == TaskStatus.ready:
                        _emit_runtime_event(
                            state, project_root, "delegate.partial", task_id=task_id
                        )
                    elif response is not None:
                        _emit_runtime_event(
                            state,
                            project_root,
                            "delegate.failed",
                            task_id=task_id,
                            error=response.block_reason,
                        )
                        # Constitution/sandbox violations are fail-closed.  A
                        # normal acceptance failure is preserved and allowed
                        # to become partial_completed with sibling results.
                        if "constitution" in (response.block_reason or "").lower():
                            safety_response = response
                        else:
                            state.status = GoalStatus.running
                            state.last_stop_reason = "acceptance_failed"
                            if task_id not in state.unfinished_work:
                                state.unfinished_work.append(task_id)
                if safety_response is not None:
                    await cancel_active()
                    save_goal_run(state, project_root)
                    _finalize_episode(state, project_root, outcome=safety_response.status.value)
                    return safety_response
                goal_no_progress.reset()
            _write_execution_checkpoint(state, project_root, list(active))
            save_goal_run(state, project_root)
            continue

        # No active work remains.  Completed/blocked/cancelled are now ready
        # for finalization; otherwise a dependency deadlock is a no-progress
        # condition rather than an infinite loop.
        if finalization.started:
            break
        if state.tasks and all(
            task.status in (TaskStatus.completed, TaskStatus.blocked, TaskStatus.cancelled)
            for task in state.tasks.values()
        ):
            break
        scheduler_decision = scheduler_state.promote_ready()
        if any(task.status == TaskStatus.ready for task in state.tasks.values()):
            goal_no_progress.reset()
            continue
        state_signature = tuple(
            sorted(
                (task.id, task.status.value, tuple(task.depends_on))
                for task in state.tasks.values()
            )
        )
        if goal_no_progress.observe(
            signature=(
                state_signature,
                tuple(sorted(state.completed_ids)),
                scheduler_decision.value,
            ),
        ):
            _mark_unfinished(state)
            state.last_stop_reason = "cross_turn_repetition"
            if finalization.start(remaining_s, no_progress=True):
                if durable_repository is not None:
                    await durable_repository.ensure_finalization_item(state.goal_id)
                state.finalization_started = True
                state.status = GoalStatus.finalizing
                _emit_runtime_event(
                    state,
                    project_root,
                    "finalization.started",
                    reason="no_progress",
                    reserve_s=finalization.reserve_s,
                )
            continue
        await asyncio.sleep(0)

    # ── G3: Finalize ───────────────────────────────────────────────────
    _mark_unfinished(state)
    durable_snapshot: dict[str, Any] | None = None
    durable_finalization_claim = None
    if durable_repository is not None and durable_worker_id is not None:
        durable_snapshot = await durable_repository.create_fanin_snapshot(state.goal_id)
        await durable_repository.ensure_finalization_item(
            state.goal_id,
            snapshot_hash=durable_snapshot["manifest_hash"],
        )
        durable_finalization_claim = await durable_repository.resume_finalization(
            state.goal_id,
            worker_id=durable_worker_id,
            lease_ttl_s=30,
        )
        if durable_finalization_claim is not None:
            await durable_repository.start(durable_finalization_claim)
            await durable_repository.checkpoint_finalization(
                durable_finalization_claim,
                snapshot_hash=durable_snapshot["manifest_hash"],
                stage="collect",
            )
    total = len(state.tasks)
    completed = sum(1 for task in state.tasks.values() if task.status == TaskStatus.completed)
    blocked = sum(1 for task in state.tasks.values() if task.status == TaskStatus.blocked)
    cancelled = sum(1 for task in state.tasks.values() if task.status == TaskStatus.cancelled)
    has_partial_work = bool(state.unfinished_work or blocked or cancelled or completed < total)
    final_status = GoalStatus.partial_completed if has_partial_work else GoalStatus.completed
    state.status = final_status

    _emit_runtime_event(state, project_root, "fanin.started")
    delegate_results: list[DelegateResult] = []
    for task in state.tasks.values():
        if task.delegate_result:
            try:
                delegate_results.append(DelegateResult.from_mapping(task.id, task.delegate_result))
            except (TypeError, ValueError):
                logger.warning(
                    "[goal_run %s] invalid delegate projection for %s", state.goal_id, task.id
                )
    fanin_batch = fan_in(delegate_results)
    _emit_runtime_event(
        state,
        project_root,
        "fanin.completed",
        complete_count=fanin_batch.complete_count,
        partial_count=fanin_batch.partial_count,
        failed_count=fanin_batch.failed_count,
        evidence_count=len(fanin_batch.evidence),
        artifact_count=len(fanin_batch.artifacts),
    )
    if durable_finalization_claim is not None and durable_snapshot is not None:
        await durable_repository.checkpoint_finalization(
            durable_finalization_claim,
            snapshot_hash=durable_snapshot["manifest_hash"],
            stage="acceptance",
            included_child_sequence=int(durable_snapshot["version"]),
            checkpoint={
                "complete_count": fanin_batch.complete_count,
                "partial_count": fanin_batch.partial_count,
                "failed_count": fanin_batch.failed_count,
            },
        )
    artifact_store = ArtifactStore(project_root, state.goal_id)
    artifact_store.ensure_layout()
    for artifact in fanin_batch.artifacts:
        artifact_store.record(artifact)
        artifact_event = {"draft": "created", "failed": "partial"}.get(
            artifact.status, artifact.status
        )
        _emit_runtime_event(
            state,
            project_root,
            f"artifact.{artifact_event}",
            path=artifact.path,
            producer=artifact.producer,
        )
    manifest_path = artifact_store.write_manifest()
    durable_result_artifact_id: str | None = None
    if durable_repository is not None:
        import hashlib

        manifest_bytes = manifest_path.read_bytes()
        durable_manifest = await durable_repository.register_artifact(
            goal_run_id=state.goal_id,
            work_item_id=durable_finalization_claim.work_item_id,
            content_uri=str(manifest_path),
            content_hash_value="sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
            size_bytes=len(manifest_bytes),
            mime_type="application/json",
            kind="final_result",
            visibility="user_visible",
            claim=durable_finalization_claim,
        )
        durable_result_artifact_id = durable_manifest["id"]

    summary_parts = [
        f"Goal: {state.goal_text if state else goal}",
        f"Status: {final_status.value}",
        f"Tasks: {completed}/{total} completed, {blocked} blocked, {cancelled} cancelled",
        f"Fan-In: {len(fanin_batch.evidence)} evidence, {len(fanin_batch.artifacts)} artifacts",
    ]
    if state.unfinished_work:
        summary_parts.append("Unfinished: " + ", ".join(state.unfinished_work))
    if state.final_summary:
        summary_parts.append(state.final_summary)
    final_summary = "\n".join(summary_parts)
    artifacts = get_goal_run_artifacts(project_root, state.goal_id if state else "temp")
    artifacts.append(str(manifest_path))
    write_final_summary(
        project_root=project_root,
        goal_id=state.goal_id if state else "temp",
        summary=final_summary,
        artifacts=artifacts,
    )
    state.final_summary = final_summary
    state.artifacts_summary = artifacts
    state.finished_at = datetime.now(UTC)
    if durable_finalization_claim is not None and durable_snapshot is not None:
        await durable_repository.checkpoint_finalization(
            durable_finalization_claim,
            snapshot_hash=durable_snapshot["manifest_hash"],
            stage="result",
            output_hash=content_hash({"answer": final_summary, "artifacts": artifacts}),
            included_child_sequence=int(durable_snapshot["version"]),
        )
        await durable_repository.complete_finalization(
            durable_finalization_claim,
            {
                "answer": final_summary,
                "artifacts": artifacts,
                "unfinished_work": state.unfinished_work,
            },
            final_status=final_status.value,
            snapshot_hash=durable_snapshot["manifest_hash"],
            result_artifact_id=durable_result_artifact_id,
            resumed=state.finalization_started,
        )
    _write_execution_checkpoint(state, project_root, [], str(manifest_path))
    save_goal_run(state, project_root)
    _emit_runtime_event(
        state,
        project_root,
        "finalization.completed",
        status=final_status.value,
        artifact_count=len(artifacts),
    )
    _finalize_episode(state, project_root, outcome=final_status.value)

    return GoalRunResponse(
        goal_id=state.goal_id if state else "temp",
        status=final_status,
        phase="finalized",
        interpretation=u.interpretation if u else goal,
        questions=None,
        goal_counts={
            "pending": sum(1 for task in state.tasks.values() if task.status == TaskStatus.pending),
            "running": len(state.running_ids),
            "completed": completed,
            "blocked": blocked,
            "cancelled": cancelled,
            "partial": int(final_status == GoalStatus.partial_completed),
        },
        summary=final_summary,
        block_reason=None if final_status != GoalStatus.blocked else state.last_stop_reason,
        artifacts=artifacts,
        next_action="none",
    )
