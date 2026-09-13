"""goal_run models — 数据模型与状态机。

与现有契约兼容：
- taskgraph.json 与 ProjectStore / DECISIONS.md 的字段名对齐
- 项目级 remember 依然由 project_ask / project_store 负责
- 本模块只关注 goal_run 内部状态机与验证逻辑
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

from server.goal_run.bot_identity import DEFAULT_BOT_ID


class GoalStatus(Enum):
    planning = "planning"
    running = "running"
    recovering = "recovering"
    finalizing = "finalizing"
    completed = "completed"
    partial_completed = "partial_completed"
    failed = "failed"
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
    # Unified delegate projection.  These fields preserve useful work even
    # when a leaf stops before acceptance succeeds.
    stop_reason: str | None = field(default=None)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    unfinished_work: list[str] = field(default_factory=list)
    delegate_result: dict[str, Any] | None = field(default=None)

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
class DelegateState:
    """Persistent projection of one delegate inside its parent GoalRun.

    P2-A §8: durable record only — no execution authority, no scheduler,
    no new GoalRun. The parent GoalRunState remains the single source of truth.
    """

    delegate_id: str
    parent_goal_run_id: str
    status: str = "running"  # running | complete | partial | failed | blocked
    request_ref: str | None = None
    result_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    attempt: int = 0
    replan: bool = False
    stopped_at: float | None = None
    # P3-A: the bot that owns this delegate. Cross-bot resume is refused.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DelegateState:
        return cls(
            delegate_id=str(value.get("delegate_id") or ""),
            parent_goal_run_id=str(value.get("parent_goal_run_id") or ""),
            status=str(value.get("status") or "running"),
            request_ref=value.get("request_ref"),
            result_ref=value.get("result_ref"),
            evidence_refs=list(value.get("evidence_refs") or []),
            attempt=int(value.get("attempt") or 0),
            replan=bool(value.get("replan", False)),
            stopped_at=value.get("stopped_at"),
            bot_id=str(value.get("bot_id") or DEFAULT_BOT_ID),
        )


@dataclass
class FanInState:
    """Persistent projection of one fan-in inside its parent GoalRun.

    P2-A §8: tracks expected vs reconciled delegates; the reconciled verdict
    is a worker outcome, never an acceptance verdict.
    """

    fanin_id: str
    expected_delegate_ids: list[str] = field(default_factory=list)
    completed_delegate_ids: list[str] = field(default_factory=list)
    partial_delegate_ids: list[str] = field(default_factory=list)
    failed_delegate_ids: list[str] = field(default_factory=list)
    blocked_delegate_ids: list[str] = field(default_factory=list)
    reconciled_result_ref: str | None = None
    completed_at: float | None = None
    # P3-A: the bot that owns this fan-in. Cross-bot resume is refused.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FanInState:
        return cls(
            fanin_id=str(value.get("fanin_id") or ""),
            expected_delegate_ids=list(value.get("expected_delegate_ids") or []),
            completed_delegate_ids=list(value.get("completed_delegate_ids") or []),
            partial_delegate_ids=list(value.get("partial_delegate_ids") or []),
            failed_delegate_ids=list(value.get("failed_delegate_ids") or []),
            blocked_delegate_ids=list(value.get("blocked_delegate_ids") or []),
            reconciled_result_ref=value.get("reconciled_result_ref"),
            completed_at=value.get("completed_at"),
            bot_id=str(value.get("bot_id") or DEFAULT_BOT_ID),
        )


@dataclass
class PlaybookState:
    """Persistent projection of a playbook run inside the SAME GoalRun.

    P2-A §4/§8: a playbook is a reusable ordered capability template. It never
    spawns a nested GoalRun and never owns acceptance.
    """

    playbook_id: str
    active_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    step_result_refs: dict[str, str] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    version: int = 1  # P2-B: pinned registry version, restored on resume.
    # P3-A: the bot that owns this playbook run. Cross-bot resume is refused.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlaybookState:
        return cls(
            playbook_id=str(value.get("playbook_id") or ""),
            active_step=value.get("active_step"),
            completed_steps=list(value.get("completed_steps") or []),
            step_result_refs=dict(value.get("step_result_refs") or {}),
            evidence_refs=list(value.get("evidence_refs") or []),
            version=int(value.get("version") or 1),
            bot_id=str(value.get("bot_id") or DEFAULT_BOT_ID),
        )


@dataclass
class RoutineState:
    """Persistent projection of a routine trigger inside its GoalRun.

    P2-A §5/§8: a routine only triggers; the persistent bot hands off to the
    canonical GoalRun path. It must not create a new GoalRun when one already
    exists for the same objective.
    """

    routine_id: str
    trigger_metadata: dict[str, Any] = field(default_factory=dict)
    started_count: int = 0
    last_trigger_ref: str | None = None
    canonical_goal_run_id: str | None = None
    # P2-C: pinned catalog version plus consumed trigger identities. A consumed
    # identity never starts again — restart-safe without a second scheduler.
    version: int = 1
    consumed_trigger_ids: list[str] = field(default_factory=list)
    # P3-A: the bot that owns this routine trigger. Cross-bot dispatch is refused.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RoutineState:
        return cls(
            routine_id=str(value.get("routine_id") or ""),
            trigger_metadata=dict(value.get("trigger_metadata") or {}),
            started_count=int(value.get("started_count") or 0),
            last_trigger_ref=value.get("last_trigger_ref"),
            canonical_goal_run_id=value.get("canonical_goal_run_id"),
            version=int(value.get("version") or 1),
            consumed_trigger_ids=list(value.get("consumed_trigger_ids") or []),
            bot_id=str(value.get("bot_id") or DEFAULT_BOT_ID),
        )


class RoutineStatus(StrEnum):
    triggered = "triggered"
    running = "running"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"
    skipped = "skipped"


@dataclass
class RoutineSpec:
    """A routine trigger. No execution authority; hands off to GoalRun.

    P2-C: exactly one target (skill, playbook, or goal template). Disabled
    routines never dispatch.
    """

    routine_id: str
    trigger_topic: str
    objective: str
    goal_run_id: str | None = None
    timeout_s: int = 3600
    max_steps: int = 10
    version: int = 1
    enabled: bool = True
    target_skill_id: str | None = None
    target_playbook_id: str | None = None
    goal_template: str = ""
    evidence_requirements: list[str] = field(default_factory=list)
    # P3-A: the bot that owns this routine. Only that bot's GoalRun may dispatch it.
    bot_id: str = DEFAULT_BOT_ID

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RoutineSpec:
        return cls(
            routine_id=str(value.get("routine_id") or ""),
            trigger_topic=str(value.get("trigger_topic") or ""),
            objective=str(value.get("objective") or ""),
            goal_run_id=value.get("goal_run_id"),
            timeout_s=int(value.get("timeout_s") or 3600),
            max_steps=int(value.get("max_steps") or 10),
            version=int(value.get("version") or 1),
            enabled=bool(value.get("enabled", True)),
            target_skill_id=value.get("target_skill_id"),
            target_playbook_id=value.get("target_playbook_id"),
            goal_template=str(value.get("goal_template") or ""),
            evidence_requirements=list(value.get("evidence_requirements") or []),
            bot_id=str(value.get("bot_id") or DEFAULT_BOT_ID),
        )


ROUTINE_TRIGGER_TOPICS = frozenset(
    {
        "schedule.trigger",
        "event.arrive",
        "user.request",
        "system.idle",
    }
)

ROUTINE_MAX_STARTED: int = int(os.environ.get("VEYA_ROUTINE_MAX_STARTED", "1"))
"""P2-A §5: max routines started; default 1 keeps one canonical GoalRun."""

P2_DURABLE_SCHEDULER_COUNT = 0
"""P2-A §8: no second durable scheduler is introduced."""

GOALRUN_DURABLE_AUTHORITY = 1
"""P2-A §8: the single canonical GoalRun is the durable source of truth."""

PERSISTENCE_PROJECTION_FIELDS = frozenset(
    {
        "delegate_states",
        "fanin_states",
        "playbook_states",
        "routine_states",
    }
)
"""P2-A §8: the only durable P2 projections — no second scheduler."""


@dataclass
class GoalRunState:
    """goal_run 的完整运行时状态（落盘以 taskgraph.json + events.jsonl）。

    权威任务图: .veya-project/goal-runs/<goal_id>/taskgraph.json
    事件日志: 同目录 events.jsonl (append-only)
    """

    goal_id: str
    goal_text: str  # 原始用户目标文本
    constitution: str = ""
    # P3-A: the persistent bot that owns this GoalRun. Every durable object
    # derived from this run inherits it; cross-bot resume is refused.
    bot_id: str = DEFAULT_BOT_ID
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
    # Execution Runtime projection. ``finalizing`` freezes new work while
    # preserving enough wall time to collect artifacts and produce a useful
    # answer. ``partial_completed`` is a real deliverable state, not a
    # disguised timeout/block.
    finalization_started: bool = False
    finalization_reserve_s: float | None = field(default=None)
    unfinished_work: list[str] = field(default_factory=list)
    last_stop_reason: str | None = field(default=None)
    runtime_checkpoint: dict[str, Any] | None = field(default=None)
    # P2-A §4: playbook — reusable ordered template in the SAME GoalRun.
    playbook_id: str | None = field(default=None)
    playbook_steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    current_playbook_step: str | None = field(default=None)
    playbook_entry_at: float | None = field(default=None)
    # P2-B: active skill/playbook pins (id+version+step) for restart resume.
    active_skill_id: str | None = field(default=None)
    active_skill_version: int | None = field(default=None)
    active_playbook_version: int | None = field(default=None)
    # P2-A §5: routines trigger only; they never own execution.
    routines: dict[str, RoutineSpec] = field(default_factory=dict)
    # P2-A §8: durable projections tied to this parent GoalRun.
    delegate_states: dict[str, DelegateState] = field(default_factory=dict)
    fanin_states: dict[str, FanInState] = field(default_factory=dict)
    playbook_states: dict[str, PlaybookState] = field(default_factory=dict)
    routine_states: dict[str, RoutineState] = field(default_factory=dict)

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
                    "stop_reason": t.stop_reason,
                    "evidence": t.evidence,
                    "assertions": t.assertions,
                    "unfinished_work": t.unfinished_work,
                    "delegate_result": t.delegate_result,
                }
            )
        return {
            "version": 2,
            "goal_id": self.goal_id,
            "bot_id": self.bot_id,
            "status": self.status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "default_assignee": self.default_assignee,
            "budget": self.budget,
            "constitution": self.constitution,
            "plan_review": self.plan_review,
            "finalization_started": self.finalization_started,
            "finalization_reserve_s": self.finalization_reserve_s,
            "unfinished_work": self.unfinished_work,
            "last_stop_reason": self.last_stop_reason,
            "runtime_checkpoint": self.runtime_checkpoint,
            "playbook_id": self.playbook_id,
            "playbook_steps": self.playbook_steps,
            "current_playbook_step": self.current_playbook_step,
            "playbook_entry_at": self.playbook_entry_at,
            "active_skill_id": self.active_skill_id,
            "active_skill_version": self.active_skill_version,
            "active_playbook_version": self.active_playbook_version,
            "routines": {routine_id: spec.to_dict() for routine_id, spec in self.routines.items()},
            "delegate_states": {
                delegate_id: item.to_dict() for delegate_id, item in self.delegate_states.items()
            },
            "fanin_states": {
                fanin_id: item.to_dict() for fanin_id, item in self.fanin_states.items()
            },
            "playbook_states": {
                playbook_id: item.to_dict() for playbook_id, item in self.playbook_states.items()
            },
            "routine_states": {
                routine_id: item.to_dict() for routine_id, item in self.routine_states.items()
            },
            "tasks": tasks_list,
        }

    @classmethod
    def from_taskgraph_json(cls, data: dict[str, Any], goal_text: str) -> GoalRunState:
        """从 taskgraph.json 反序列化（用于 resume）。"""
        state = cls(goal_id=data.get("goal_id", ""), goal_text=goal_text)
        state.bot_id = str(data.get("bot_id") or DEFAULT_BOT_ID)
        state.status = GoalStatus(data.get("status", "planning"))
        state.default_assignee = data.get("default_assignee", "hicode")
        state.budget = data.get("budget", state.budget)
        state.constitution = data.get("constitution", "") or ""
        state.plan_review = data.get("plan_review")
        for field_name in ("started_at", "finished_at"):
            raw_timestamp = data.get(field_name)
            if raw_timestamp:
                with contextlib.suppress(ValueError):
                    setattr(state, field_name, datetime.fromisoformat(str(raw_timestamp)))
        state.finalization_started = bool(data.get("finalization_started", False))
        state.finalization_reserve_s = data.get("finalization_reserve_s")
        state.unfinished_work = list(data.get("unfinished_work") or [])
        state.last_stop_reason = data.get("last_stop_reason")
        state.runtime_checkpoint = data.get("runtime_checkpoint")
        # P2-A §4/§5/§8: additive projections; old files without them load fine.
        state.playbook_id = data.get("playbook_id")
        state.playbook_steps = dict(data.get("playbook_steps") or {})
        state.current_playbook_step = data.get("current_playbook_step")
        state.playbook_entry_at = data.get("playbook_entry_at")
        state.active_skill_id = data.get("active_skill_id")
        state.active_skill_version = data.get("active_skill_version")
        state.active_playbook_version = data.get("active_playbook_version")
        state.routines = {
            routine_id: RoutineSpec.from_dict(spec)
            for routine_id, spec in dict(data.get("routines") or {}).items()
            if isinstance(spec, dict)
        }
        state.delegate_states = {
            delegate_id: DelegateState.from_dict(item)
            for delegate_id, item in dict(data.get("delegate_states") or {}).items()
            if isinstance(item, dict)
        }
        state.fanin_states = {
            fanin_id: FanInState.from_dict(item)
            for fanin_id, item in dict(data.get("fanin_states") or {}).items()
            if isinstance(item, dict)
        }
        state.playbook_states = {
            playbook_id: PlaybookState.from_dict(item)
            for playbook_id, item in dict(data.get("playbook_states") or {}).items()
            if isinstance(item, dict)
        }
        state.routine_states = {
            routine_id: RoutineState.from_dict(item)
            for routine_id, item in dict(data.get("routine_states") or {}).items()
            if isinstance(item, dict)
        }

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
                stop_reason=td.get("stop_reason"),
                evidence=list(td.get("evidence") or []),
                assertions=list(td.get("assertions") or []),
                unfinished_work=list(td.get("unfinished_work") or []),
                delegate_result=td.get("delegate_result"),
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
        return self.status in (
            GoalStatus.completed,
            GoalStatus.partial_completed,
            GoalStatus.failed,
            GoalStatus.blocked,
            GoalStatus.cancelled,
        )


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
