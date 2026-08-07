"""veya_loop.omodul.long_task_state — 长程任务状态内核 (单一来源转发)。

转发 omodul.long_task_goal.GoalKernel 投影状态机 (事件溯源):
goal/todo/gate/evidence/handoff/quota 从事件流重建, 支持跨重启恢复、
operator/auto gate、截断检测、续跑决策 (next_action)。

装配示例::

    from veya_loop.obase.loop_event_store import AppendOnlyEventStore
    from veya_loop.omodul.long_task_state import GoalKernel

    store = AppendOnlyEventStore(Path("~/.veya/loops/g1/events.jsonl"))
    kernel = GoalKernel(store, goal_id="g1").rebuild()   # 跨天恢复
    action = kernel.next_action()                         # 续跑决策
"""

from .._assembly import omodul as _load_omodul

_omodul = _load_omodul()

GoalKernel = _omodul.long_task_goal.GoalKernel
GoalKernelError = _omodul.long_task_goal.GoalKernelError
Todo = _omodul.long_task_goal.Todo
Gate = _omodul.long_task_goal.Gate
Goal = _omodul.long_task_goal.Goal
Evidence = _omodul.long_task_goal.Evidence
Handoff = _omodul.long_task_goal.Handoff
QuotaView = _omodul.long_task_goal.QuotaView
IntegrityResult = _omodul.long_task_goal.IntegrityResult

EVENT_GOAL_ADDED = _omodul.long_task_goal.EVENT_GOAL_ADDED
EVENT_TODO_UPDATED = _omodul.long_task_goal.EVENT_TODO_UPDATED
EVENT_GATE_REQUIRED = _omodul.long_task_goal.EVENT_GATE_REQUIRED
EVENT_GATE_RESOLVED = _omodul.long_task_goal.EVENT_GATE_RESOLVED
EVENT_EVIDENCE_APPENDED = _omodul.long_task_goal.EVENT_EVIDENCE_APPENDED
EVENT_HANDOFF_RECORDED = _omodul.long_task_goal.EVENT_HANDOFF_RECORDED

TODO_OPEN = _omodul.long_task_goal.TODO_OPEN
TODO_DONE = _omodul.long_task_goal.TODO_DONE
TODO_BLOCKED = _omodul.long_task_goal.TODO_BLOCKED
TODO_DEFERRED = _omodul.long_task_goal.TODO_DEFERRED
GATE_KIND_OPERATOR = _omodul.long_task_goal.GATE_KIND_OPERATOR
GATE_KIND_AUTO = _omodul.long_task_goal.GATE_KIND_AUTO

__all__ = [
    "EVENT_EVIDENCE_APPENDED",
    "EVENT_GATE_REQUIRED",
    "EVENT_GATE_RESOLVED",
    "EVENT_GOAL_ADDED",
    "EVENT_HANDOFF_RECORDED",
    "EVENT_TODO_UPDATED",
    "GATE_KIND_AUTO",
    "GATE_KIND_OPERATOR",
    "TODO_BLOCKED",
    "TODO_DEFERRED",
    "TODO_DONE",
    "TODO_OPEN",
    "Evidence",
    "Gate",
    "Goal",
    "GoalKernel",
    "GoalKernelError",
    "Handoff",
    "IntegrityResult",
    "QuotaView",
    "Todo",
]
