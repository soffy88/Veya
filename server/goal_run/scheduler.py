"""goal_run scheduler — 调度器：ready queue、并发控制、预算检查。

实现要点：
- 每 tick：检查预算超时 → blocked
- 将 deps 满足的 pending → ready
- 在 concurrency 限制下取 ready → running
- 并发默认 1 (串行)，以后再升
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Set, Dict, List

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus, GoalRunResponse


class SchedulerDecision(Enum):
    promote = "promote"      # 有任务可以 promote
    none = "none"            # 没有就绪任务
    timeout = "timeout"      # 超时
    all_done = "all_done"    # 所有任务已完成


@dataclass
class SchedulerState:
    """调度器运行时状态（在 runner.py 中实时维护）。"""

    state: GoalRunState
    max_concurrent: int = 1  # v0.1 默认串行
    budget_consumed_s: float = 0.0  # 已消耗时长

    def check_budget(self, elapsed_s: float) -> bool:
        """检查是否超时。返回 True 表示仍在预算内。"""
        return elapsed_s <= self.state.budget.get("max_wall_s", 7200)

    def can_start_more(self) -> bool:
        """检查是否还有槽位可以启动新任务。"""
        return len(self.state.running_ids) < self.max_concurrent

    def promote_ready(self) -> SchedulerDecision:
        """每 tick 调度一次：promote deps 滿足的 pending → ready。

        返回决策类型。
        """
        # 收集已完成的 id
        completed_ids = self.state.completed_ids
        running_ids = self.state.running_ids

        # 遍历所有 pending 任务，找出 deps 满足的
        promoted = 0
        for tn_id, tn in self.state.tasks.items():
            if tn.status == TaskStatus.pending and tn.can_run_now(running_ids, completed_ids):
                tn.status = TaskStatus.ready
                promoted += 1

        if promoted > 0:
            return SchedulerDecision.promote

        # 检查是否所有非阻塞任务都已完成
        pending_count = sum(1 for tn in self.state.tasks.values() if tn.status == TaskStatus.pending)
        if pending_count == 0 and len(self.state.running_ids) == 0:
            return SchedulerDecision.all_done

        # 检查是否还有可运行的任务（ready 状态）
        ready_count = sum(1 for tn in self.state.tasks.values() if tn.status == TaskStatus.ready)
        if ready_count > 0:
            return SchedulerDecision.promote  # 已有 ready 任务，调度员可直接取走

        # 检查是否所有运行中的任务都卡死或无法继续（没有 pending 可提升）
        # 即没有就绪任务可调度
        if pending_count > 0 and ready_count == 0 and len(self.state.running_ids) > 0:
            # 有 pending 但都在 running，没有就绪 -> 等待 running 任务完成
            return SchedulerDecision.none

        return SchedulerDecision.none


def pick_next_tasks(
    state: GoalRunState,
    max_concurrent: int,
    running_ids: Set[str],
) -> List[TaskNode]:
    """从 ready 任务中选出最多 max_concurrent 个进入 running。

    实现要点：
    - 按准备就绪顺序（或随机/优先级）选任务
    - 更新任务状态为 running
    - 返回被选中的任务列表
    """
    ready_tasks = [
        tn for tn_id, tn in state.tasks.items()
        if tn.status == TaskStatus.ready and tn.id not in running_ids
    ]

    if not ready_tasks:
        return []

    # 简单的 FIFO: 按任务 id 排序 (也可按优先级)
    ready_tasks.sort(key=lambda tn: tn.id)

    to_run = ready_tasks[:max_concurrent]
    for tn in to_run:
        tn.status = TaskStatus.running
        state.running_ids.add(tn.id)
        state.completed_ids.discard(tn.id)  # 确保不重复

    return to_run


async def check_and_update_budget(
    state: GoalRunState,
    elapsed_s: float,
    scheduler_state: SchedulerState,
) -> GoalRunResponse | None:
    """检查预算是否耗尽，若超时返回 block 响应。

    实现要点：
    - 若超时 max_wall_s → goal blocked budget_exceeded
    - 更新 scheduler_state.budget_consumed_s
    """
    if not scheduler_state.check_budget(elapsed_s):
        from server.goal_run.models import GoalStatus

        state.status = GoalStatus.blocked
        return GoalRunResponse(
            goal_id=state.goal_id,
            status=GoalStatus.blocked,
            phase="running",
            interpretation=None,
            questions=None,
            goal_counts=state.snapshot_running(),
            summary=None,
            block_reason="budget_exceeded",
            artifacts=None,
            next_action="none",
        )

    return None