"""Canonical GoalRun pre-admission for remote execution.

This module only constructs the existing :class:`GoalRunState` and persists it
through the existing GoalRun store. It is not a scheduler and does not launch
providers. A pre-admitted run is therefore a normal, resumable GoalRun whose
task is still pending execution.

Admission also admits the run's context projection through the shared
:class:`~server.context_gateway.ContextGateway` (P0-06), fail-open: context is
an automatic side effect of admission, never a reason to fail a run.

The admitted projection is recorded once into the run's own append-only event
log, and this module owns both halves of that record: :func:`context_projection`
is the single read path for resolve / cite / explain / audit, reloading the
*same* projection through the *same* gateway after a process restart. There is
no second ContextGateway and no second context state.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from server.context_gateway import (
    ContextGateway,
    ContextItem,
    ContextPolicy,
    ContextProjection,
    ContextScope,
)
from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.store import append_event, load_goal_run, read_events, save_goal_run
from server.knowledge_binding import BindingScope, KnowledgeBinding, binding_applies, new_binding

logger = logging.getLogger("veya.goal_run")

_ADMISSION_LOCK = threading.RLock()

#: 进程级共享 ContextGateway (P0-06)。上下文准入是 GoalRun admission 的自动旁路,
#: 不是一次调用内的临时对象: 投影必须在准入之后仍可被回读 (iterate/cite/解释),
#: 所以只有一个实例, 不随每次 dispatch 重建。
_CONTEXT_GATEWAY = ContextGateway()

#: admission 阶段为一次派发运行投影的候选 scope (按优先级)。
_ADMISSION_SCOPES: tuple[ContextScope, ...] = (
    ContextScope.GOAL,
    ContextScope.PROJECT,
    ContextScope.SESSION,
)

#: 让候选 scope 真正可用的知识绑定 (P1-06)。适用性 = scope 兼容 + binding,
#: 不是语义相似度: 被发现但无绑定覆盖的 scope 不进投影。
_ADMISSION_BINDINGS: tuple[KnowledgeBinding, ...] = (
    new_binding("goal_text", BindingScope.GOAL),
    new_binding("project_state", BindingScope.PROJECT),
)

#: 一次 admission 的投影 token 预算 (与 ContextPolicy 默认一致)。
_CONTEXT_BUDGET_TOKENS = 8000

#: 落盘的准入记录事件类型 —— 投影唯一的 durable 面, 走该 GoalRun 已有的
#: append-only 事件流 (不为上下文另开一个 store)。
_CONTEXT_ADMITTED_EVENT = "context_projection_admitted"

#: 回读时扫描的事件条数上限 —— 是上限, 不是窗口。事件是追加的, 准入记录写在
#: 最前面, 所以必须扫到文件头; 超出这个量级的日志按"没找到"处理而不是只看尾部。
_EVENT_SCAN_LIMIT = 1 << 20


def context_gateway() -> ContextGateway:
    """唯一的 ContextGateway 权威 (解释/审计/回读都走这一个实例)。"""
    return _CONTEXT_GATEWAY


def context_projection(*, goal_run_id: str, project_root: str = "") -> ContextProjection | None:
    """resolve / cite / explain / audit 共用的唯一读出口 (P0-06)。

    进程内直接给出共享 gateway 准入时持久的那个投影; 进程重启后 gateway 是空的,
    就从该 GoalRun 的 durable 准入记录还原**同一个**投影再交回同一个 gateway ——
    读面与写面永远是同一个 authority, 不产生第二份 context state。
    """
    projection = _CONTEXT_GATEWAY._projections.get(goal_run_id)
    if projection is not None:
        return projection
    if not project_root:
        return None
    record = _load_context_projection_record(project_root, goal_run_id)
    if record is None:
        return None
    # ContextGateway 是冻结面 (没有公开的 reload seam), 所以还原直接装回它自己的
    # 索引, 并保留 projection_id —— 重启前后是同一个投影, 不是副本也不是新铸的身份。
    _CONTEXT_GATEWAY._projections[goal_run_id] = record
    return record


def _load_context_projection_record(
    project_root: str, goal_run_id: str
) -> ContextProjection | None:
    """从该 GoalRun 的 durable 事件流还原准入时的那个投影 (重启后的唯一来源)。"""
    try:
        events = read_events(project_root, goal_run_id, limit=_EVENT_SCAN_LIMIT)
    except Exception as exc:
        logger.warning(
            "[GoalRun] 上下文记录回读失败 %s: %s: %s", goal_run_id, type(exc).__name__, exc
        )
        return None
    for event in events:
        if event.get("type") != _CONTEXT_ADMITTED_EVENT:
            continue
        payload = event.get("projection") or {}
        items: list[ContextItem] = []
        for raw in payload.get("items") or []:
            fields = {key: raw[key] for key in ContextItem.__dataclass_fields__ if key in raw}
            # to_dict() 把 scope 存成字符串, 还原时必须回到枚举, 否则读面拿到的
            # 是半个 ContextItem。
            fields["scope"] = ContextScope(fields["scope"])
            items.append(ContextItem(**fields))
        return ContextProjection(
            projection_id=str(payload.get("projection_id") or ""),
            goal_run_id=goal_run_id,
            items=items,
            budget_tokens=int(payload.get("budget_tokens") or 0),
        )
    return None


def _admit_context(*, goal_run_id: str, project_root: str) -> ContextProjection | None:
    """为被准入的运行 admit 一个 ContextProjection (P0-06) + binding 过滤 (P1-06)。

    这条路径不依赖模型记得去调搜索工具: 准入即发生。**fail-open** ——
    上下文建不起来只记录, 绝不返回失败, 也不抛出 (调用点已在失败即关门的
    ``try`` 之外)。
    """
    try:
        scopes = [
            scope
            for scope in _ADMISSION_SCOPES
            if any(
                binding_applies(binding, context_scope=BindingScope(scope.value))
                for binding in _ADMISSION_BINDINGS
            )
        ]
        projection = _CONTEXT_GATEWAY.admit(
            goal_run_id=goal_run_id,
            policy=ContextPolicy(
                goal_run_id=goal_run_id,
                required_scopes=scopes,
                budget_tokens=_CONTEXT_BUDGET_TOKENS,
            ),
        )
    except Exception as exc:
        logger.warning("[GoalRun] 上下文准入跳过 %s: %s: %s", goal_run_id, type(exc).__name__, exc)
        return None
    if projection is not None:
        _record_context_projection(project_root, projection)
    return projection


def _record_context_projection(project_root: str, projection: ContextProjection) -> None:
    """把准入出来的投影写进该 GoalRun 的 durable 事件流 (fail-open)。

    落盘面只有这一条: 回读/解释/审计都从它还原, 所以同一份投影不会再有第二个
    state。写不进去也不影响已成立的内存投影 —— 旁路, 不是准入门。
    """
    try:
        append_event(
            project_root,
            projection.goal_run_id,
            {"type": _CONTEXT_ADMITTED_EVENT, "projection": projection.to_dict()},
        )
    except Exception as exc:
        logger.warning(
            "[GoalRun] 上下文投影落盘失败 %s: %s: %s",
            projection.goal_run_id,
            type(exc).__name__,
            exc,
        )


@dataclass(frozen=True)
class PreAdmission:
    state: GoalRunState
    task_ids: tuple[str, ...]

    @property
    def goal_run_id(self) -> str:
        return self.state.goal_id

    @property
    def goal_task_id(self) -> str:
        return self.task_ids[0]


def _iter_runs(project_root: str) -> list[GoalRunState]:
    root = Path(project_root) / ".veya-project" / "goal-runs"
    if not root.is_dir():
        return []
    result: list[GoalRunState] = []
    for taskgraph in sorted(root.glob("*/taskgraph.json")):
        state = load_goal_run(project_root, taskgraph.parent.name)
        if state is not None:
            result.append(state)
    return result


def find_by_dispatch_id(project_root: str, dispatch_id: str) -> PreAdmission | None:
    """Resolve the canonical pre-admitted run after reconnect/restart."""
    with _ADMISSION_LOCK:
        for state in _iter_runs(project_root):
            if state.dispatch_id != dispatch_id:
                continue
            return PreAdmission(state, tuple(state.tasks))
    return None


def reconcile_unbound_runs(project_root: str, *, reason: str) -> int:
    """Fail closed runs persisted before an execution identity was bound.

    This is the recovery half of the pre-admission transaction.  A backend
    crash after GoalRun/task persistence but before execution persistence has
    no execution record to hand to the execution manager; leaving the run
    pending would create an orphan that could be mistaken for live work.
    """
    roots = [Path(project_root)]
    roots.extend(path for path in Path(project_root).iterdir() if path.is_dir())
    reconciled = 0
    with _ADMISSION_LOCK:
        for root in roots:
            for state in _iter_runs(str(root)):
                if (
                    not state.dispatch_id
                    or state.execution_id
                    or state.status != GoalStatus.pending_execution
                ):
                    continue
                fail_pre_admission(project_root=str(root), goal_run_id=state.goal_id, reason=reason)
                reconciled += 1
    return reconciled


def create_pending_run(
    *,
    project_root: str,
    dispatch_id: str,
    session_id: str,
    requested_executor: str,
    tasks: list[dict[str, object]],
) -> PreAdmission:
    """Create one real canonical GoalRun and its pending task nodes.

    The operation is idempotent on ``dispatch_id``. Any failure is recorded as
    a terminal failed GoalRun before being re-raised, so it cannot leave an
    orphan pending run that could later launch without a durable task.
    """
    if not tasks:
        raise ValueError("pre-admission requires at least one task")
    with _ADMISSION_LOCK:
        existing = find_by_dispatch_id(project_root, dispatch_id)
        if existing is not None:
            return existing

        goal_id = f"goal_{uuid4().hex}"
        state = GoalRunState(
            goal_id=goal_id,
            goal_text=str(tasks[0].get("task") or "remote worker dispatch"),
            status=GoalStatus.pending_execution,
            default_assignee=str(tasks[0].get("worker") or requested_executor),
            dispatch_id=dispatch_id,
            session_id=session_id,
            requested_executor=requested_executor,
        )
        try:
            for index, item in enumerate(tasks):
                task_id = f"task_{uuid4().hex}"
                state.tasks[task_id] = TaskNode(
                    id=task_id,
                    title=f"Remote worker {index + 1}",
                    instruction=str(item.get("task") or ""),
                    acceptance=["remote provider returned a successful result"],
                    depends_on=[],
                    assignee=str(item.get("worker") or requested_executor),
                    status=TaskStatus.pending,
                    parallel=len(tasks) > 1,
                )
            save_goal_run(state, project_root)
            append_event(
                project_root,
                goal_id,
                {
                    "type": "goal_run_pre_admitted",
                    "goal_id": goal_id,
                    "dispatch_id": dispatch_id,
                    "session_id": session_id,
                    "requested_executor": requested_executor,
                    "task_ids": list(state.tasks),
                    "created_at": time.time(),
                },
            )
        except Exception as exc:
            state.status = GoalStatus.failed
            state.last_stop_reason = f"pre_admission_failed: {type(exc).__name__}: {exc}"
            try:
                save_goal_run(state, project_root)
                append_event(
                    project_root,
                    goal_id,
                    {
                        "type": "goal_run_pre_admission_failed",
                        "goal_id": goal_id,
                        "dispatch_id": dispatch_id,
                        "error": state.last_stop_reason,
                    },
                )
            except Exception:
                # Preserve the original failure; no provider may launch.
                pass
            raise

    # 持久化已落盘, 走到这里本次 admission 已成立 —— 之后的上下文准入 (P0-06)
    # 是旁路, 失败只记录: 已持久化的 GoalRun 绝不因上下文建不起来被判成失败。
    _admit_context(goal_run_id=state.goal_id, project_root=project_root)
    return PreAdmission(state, tuple(state.tasks))


def bind_execution(
    pre_admission: PreAdmission, execution_id: str, project_root: str
) -> GoalRunState:
    """Persist the execution identity into the canonical pre-admitted run."""
    state = load_goal_run(project_root, pre_admission.goal_run_id)
    if state is None or state.dispatch_id != pre_admission.state.dispatch_id:
        raise RuntimeError("canonical pre-admitted GoalRun disappeared")
    if state.execution_id not in (None, execution_id):
        raise RuntimeError("dispatch is already bound to another execution")
    state.execution_id = execution_id
    save_goal_run(state, project_root)
    append_event(
        project_root,
        state.goal_id,
        {"type": "execution_admitted", "goal_id": state.goal_id, "execution_id": execution_id},
    )
    return state


def reconcile_task(
    *,
    project_root: str,
    goal_run_id: str,
    goal_task_id: str,
    outcome: str,
    summary: str = "",
    reason: str | None = None,
) -> GoalRunState:
    """Reconcile one pre-admitted task through the canonical GoalRun store."""
    state = load_goal_run(project_root, goal_run_id)
    if state is None or goal_task_id not in state.tasks:
        raise RuntimeError("canonical GoalRun/task disappeared during reconciliation")
    task = state.tasks[goal_task_id]
    normalized = str(outcome).lower()
    if normalized == "completed":
        # Verifier gate (Phase B): an execution COMPLETED is not a GoalRun DONE.
        # Rule + mechanical checks run synchronously here; LLM judgement stays
        # in the async verify_task path. A task that fails its acceptance
        # criteria is blocked, never completed, and the goal cannot reach DONE.
        # Fail closed: a verifier that raises is a failed verification, never
        # a pass by default. Without this, a task would rest in running while
        # its execution already ended, and the goal could never converge.
        try:
            from server.goal_run.verify import _mechanical_verify, _rule_check

            rule_ok, rule_reason = _rule_check(task, summary or "", project_root)
            mech = _mechanical_verify(task, summary or "")
        except Exception as exc:
            task.status = TaskStatus.blocked
            task.block_reason = f"verifier error: {type(exc).__name__}: {exc}"
        else:
            if not rule_ok:
                task.status = TaskStatus.blocked
                task.block_reason = rule_reason or "acceptance rule check failed"
            elif mech is not None and not mech[0]:
                task.status = TaskStatus.blocked
                task.block_reason = mech[1] or "acceptance mechanical check failed"
            else:
                task.status = TaskStatus.completed
                state.completed_ids.add(goal_task_id)
    elif normalized == "cancelled":
        task.status = TaskStatus.cancelled
        task.stop_reason = reason or "cancelled"
    else:
        task.status = TaskStatus.blocked
        task.block_reason = reason or summary or normalized
    state.running_ids.discard(goal_task_id)
    task.execute_result = summary or task.execute_result
    if all(item.status == TaskStatus.completed for item in state.tasks.values()):
        state.status = GoalStatus.completed
        # Verifier is the single completion authority: DONE requires every
        # task to have passed its acceptance gate above. The verdict is always
        # recorded, never absent.
        state.acceptance_verdict = "ACCEPT"
    elif all(item.status == TaskStatus.cancelled for item in state.tasks.values()):
        state.status = GoalStatus.cancelled
        state.acceptance_verdict = "CANCELLED"
    elif all(
        item.status in {TaskStatus.completed, TaskStatus.blocked, TaskStatus.cancelled}
        for item in state.tasks.values()
    ):
        state.status = GoalStatus.failed
        state.acceptance_verdict = "PARTIAL"
    save_goal_run(state, project_root)
    append_event(
        project_root,
        goal_run_id,
        {
            "type": "goal_task_reconciled",
            "goal_id": goal_run_id,
            "task_id": goal_task_id,
            "outcome": normalized,
            "reason": reason,
        },
    )
    return state


def mark_task_running(*, project_root: str, goal_run_id: str, goal_task_id: str) -> GoalRunState:
    """Persist the canonical task transition immediately before provider start."""
    state = load_goal_run(project_root, goal_run_id)
    if state is None or goal_task_id not in state.tasks:
        raise RuntimeError("canonical GoalRun/task disappeared before worker launch")
    task = state.tasks[goal_task_id]
    task.status = TaskStatus.running
    state.running_ids.add(goal_task_id)
    state.status = GoalStatus.running
    save_goal_run(state, project_root)
    append_event(
        project_root,
        goal_run_id,
        {"type": "goal_task_started", "goal_id": goal_run_id, "task_id": goal_task_id},
    )
    return state


def fail_pre_admission(*, project_root: str, goal_run_id: str, reason: str) -> GoalRunState:
    """Terminalize a pre-admitted run that never reached worker launch."""
    state = load_goal_run(project_root, goal_run_id)
    if state is None:
        raise RuntimeError("canonical pre-admitted GoalRun disappeared during recovery")
    for task in state.tasks.values():
        if task.status not in {TaskStatus.completed, TaskStatus.cancelled}:
            task.status = TaskStatus.blocked
            task.block_reason = reason
    state.running_ids.clear()
    state.status = GoalStatus.failed
    state.last_stop_reason = reason
    save_goal_run(state, project_root)
    append_event(
        project_root,
        goal_run_id,
        {"type": "pre_admission_reconciled", "goal_id": goal_run_id, "reason": reason},
    )
    return state
