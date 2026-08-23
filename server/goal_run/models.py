"""goal_run models — 数据模型与状态机。

与现有契约兼容：
- taskgraph.json 与 ProjectStore / DECISIONS.md 的字段名对齐
- 项目级 remember 依然由 project_ask / project_store 负责
- 本模块只关注 goal_run 内部状态机与验证逻辑
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any


class GoalStatus(Enum):
    planning = "planning"
    running = "running"
    completed = "completed"
    awaiting_user = "awaiting_user"
    blocked = "blocked"
    cancelled = "cancelled"


class TaskStatus(Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    verifying = "verifying"
    completed = "completed"
    blocked = "blocked"
    cancelled = "cancelled"


@dataclass
class TaskNode:
    """单个叶子任务节点（taskgraph.json 中的一个 entry）。

    与现有 taskgraph.json schema 对齐，仅在此处添加运行时状态字段。
    """

    id: str
    title: str
    instruction: str  # 给工人的明确指令
    acceptance: list[str]  # 可观察条件列表
    depends_on: list[str]  # 前置 task id 列表 (无环)
    assignee: str  # 默认从 goal 默认值或配置中决定
    status: TaskStatus = field(default=TaskStatus.pending)
    retries: int = field(default=0)
    leaf_task_id: str | None = field(default=None)  # 子任务 id (若为复合任务)
    verify_summary: str | None = field(default=None)
    block_reason: str | None = field(default=None)
    artifacts: list[str] = field(default_factory=list)  # 本任务产物路径索引
    execute_result: str | None = field(default=None)  # leaf 执行输出
    # 执行侧分支(对标"Pi"清单 P2, 见 memory project_veya_pi_gap_audit 步骤8):
    # 每次验收失败重试用 SessionTreeMgr.branch() 在失败节点下开新叶, 而不是
    # 原地覆盖 instruction——失败尝试留在树里可查, 不是被字符串拼接悄悄吞掉。
    session_tree_sid: str | None = field(default=None)
    session_tree_leaf: str | None = field(default=None)
    # 双轴代码审查(mattpocock/skills code-review 内化, 见 memory
    # project_veya_pi_gap_audit): {"standards": {...}, "spec": {...}} 或 None
    # (没跑/diff为空)。advisory only——不影响 verify 是否 passed, 只是记录下来
    # 供人/下游查看, LLM 判断是假设不是证据。
    review_findings: dict[str, Any] | None = field(default=None)
    # smart-ralph [P] 并行标记内化(见 memory project_veya_pi_gap_audit): tasks.md
    # 里显式标 [P] 的任务才会被 scheduler 并发调度, 没标的严格串行——这是任务
    # 作者(g1_plan/人工写的 tasks.md)的声明, veya 不自己猜哪些任务"看起来"能
    # 并行(猜错了两个任务同时改同一批文件是真实数据损坏风险)。
    parallel: bool = field(default=False)

    def ready_condition_met(self, completed_ids: set[str]) -> bool:
        """deps 全 completed 则就 ready。空 deps 始终满足。"""
        if not self.depends_on:
            return True
        return all(d in completed_ids for d in self.depends_on)

    def can_run_now(self, running_ids: set[str], completed_ids: set[str]) -> bool:
        """未运行且 deps 满足则可运行。"""
        if self.status != TaskStatus.pending:
            return False
        if self.id in running_ids:
            return False
        return self.ready_condition_met(completed_ids)


@dataclass
class GoalRunState:
    """goal_run 的完整运行时状态（落盘以 taskgraph.json + events.jsonl）。

    权威任务图: .veya-project/goal-runs/<goal_id>/taskgraph.json
    事件日志: 同目录 events.jsonl (append-only)
    """

    goal_id: str
    goal_text: str  # 原始用户目标文本
    constitution: str = ""
    status: GoalStatus = GoalStatus.planning
    default_assignee: str = "hicode"
    budget: dict[str, int] = field(
        default_factory=lambda: {
            "max_wall_s": 7200,  # 总时长上限(s)
            "max_leaf_tasks": 40,  # 最大叶子任务数
            "max_retries_per_task": 2,  # 单任务最大重试次数
        }
    )
    tasks: dict[str, TaskNode] = field(default_factory=dict)
    completed_ids: set[str] = field(default_factory=set)
    running_ids: set[str] = field(default_factory=set)
    started_at: datetime | None = field(default=None)
    finished_at: datetime | None = field(default=None)

    # G3 Finalize 产物
    final_summary: str | None = field(default=None)
    artifacts_summary: list[str] = field(default_factory=list)
    # 计划前置双轴审查(oh-my-openagent orchestration 内化, 见 memory
    # project_veya_pi_gap_audit): G1 出图后、G2 执行前跑一遍, blocked 时状态
    # 停在 awaiting_user, 不进 G2。None = 还没审过。
    plan_review: dict[str, Any] | None = field(default=None)

    def to_taskgraph_json(self) -> dict[str, Any]:
        """转为 taskgraph.json 格式（用于落盘/序列化）。"""
        tasks_list = []
        for t in self.tasks.values():
            tasks_list.append(
                {
                    "id": t.id,
                    "title": t.title,
                    "instruction": t.instruction,
                    "acceptance": t.acceptance,
                    "depends_on": t.depends_on,
                    "assignee": t.assignee,
                    "status": t.status.value,
                    "retries": t.retries,
                    "leaf_task_id": t.leaf_task_id,
                    "verify_summary": t.verify_summary,
                    "block_reason": t.block_reason,
                    "artifacts": t.artifacts,
                    "execute_result": t.execute_result,
                    "session_tree_sid": t.session_tree_sid,
                    "session_tree_leaf": t.session_tree_leaf,
                    "review_findings": t.review_findings,
                    "parallel": t.parallel,
                }
            )
        return {
            "version": 1,
            "goal_id": self.goal_id,
            "status": self.status.value,
            "default_assignee": self.default_assignee,
            "budget": self.budget,
            "constitution": self.constitution,
            "plan_review": self.plan_review,
            "tasks": tasks_list,
        }

    @classmethod
    def from_taskgraph_json(cls, data: dict[str, Any], goal_text: str) -> "GoalRunState":
        """从 taskgraph.json 反序列化（用于 resume）。"""
        state = cls(goal_id=data.get("goal_id", ""), goal_text=goal_text)
        state.status = GoalStatus(data.get("status", "planning"))
        state.default_assignee = data.get("default_assignee", "hicode")
        state.budget = data.get("budget", state.budget)
        state.constitution = data.get("constitution", "") or ""
        state.plan_review = data.get("plan_review")

        for td in data.get("tasks", []):
            tn = TaskNode(
                id=td["id"],
                title=td["title"],
                instruction=td.get("instruction", ""),
                acceptance=td.get("acceptance", []),
                depends_on=td.get("depends_on", []),
                assignee=td.get("assignee", state.default_assignee),
                status=TaskStatus(td.get("status", "pending")),
                retries=td.get("retries", 0),
                leaf_task_id=td.get("leaf_task_id"),
                verify_summary=td.get("verify_summary"),
                block_reason=td.get("block_reason"),
                artifacts=td.get("artifacts", []),
                execute_result=td.get("execute_result"),
                session_tree_sid=td.get("session_tree_sid"),
                session_tree_leaf=td.get("session_tree_leaf"),
                review_findings=td.get("review_findings"),
                parallel=bool(td.get("parallel", False)),
            )
            state.tasks[tn.id] = tn

        return state

    def snapshot_running(self) -> dict[str, Any]:
        """返回当前正在运行/等待的任务快照（用于前端轮询或 resume）。"""
        return {
            "status": self.status.value,
            "running_ids": list(self.running_ids),
            "completed_ids": list(self.completed_ids),
            "tasks": {
                tid: {
                    "id": tn.id,
                    "status": tn.status.value,
                    "title": tn.title,
                    "retries": tn.retries,
                    "block_reason": tn.block_reason,
                }
                for tid, tn in self.tasks.items()
                if tn.status in (TaskStatus.running, TaskStatus.ready, TaskStatus.pending)
            },
        }

    def is_terminal(self) -> bool:
        return self.status in (GoalStatus.completed, GoalStatus.blocked, GoalStatus.cancelled)


@dataclass
class GoalRunResponse:
    """project_run_goal 返回的统一响应。"""

    goal_id: str
    status: GoalStatus
    phase: str  # understood_ask | planning | running | finalized | rejected
    interpretation: str | None = None
    questions: list[str] | None = None  # G0 ask 阶段的问题
    goal_counts: dict[str, int] | None = None  # {pending, running, completed, blocked}
    summary: str | None = None  # G3 final summary
    block_reason: str | None = None  # 若 blocked，阻塞原因
    artifacts: list[str] | None = None  # 汇总产物路径
    next_action: str | None = None  # wait | inspect_tasks | none
