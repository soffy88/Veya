"""loop-plane domain.state.service — Goal 状态服务（写事件 + 读投影）。

对应 SPEC §4.2 State API（原 plan_todo + state_kernel 语义）:
    create_goal  ≡ create_plan
    update_todo  ≡ update_todo
    claim        ≡ todo_claim（未过期再 claim → 拒绝, fail-closed）
    should_run   ≡ quota_should_run
    gate_check   ≡ gate_check
    terminal_check ≡ terminal_gate_check（只返回「需审批」建议, 不自动执行）
    spend        ≡ quota_spend_slot
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.state.projectors import normalize_status, project_goal_state
from app.infra.event_store import EventStore, new_id

# 与现 state_kernel 一致的 lease 默认/上限
DEFAULT_LEASE_MIN = 45
MAX_LEASE_MIN = 24 * 60
# 与现 quota 一致的默认预算
DEFAULT_QUOTA = 8


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _now_ts() -> float:
    return time.time()


def _lease_until(lease_min: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=lease_min)).isoformat()


class GoalService:
    """Goal/Todo/Gate/Quota 状态服务（事件溯源）。"""

    def __init__(self, store: EventStore, *, default_quota: int = DEFAULT_QUOTA) -> None:
        self._store = store
        self._default_quota = default_quota

    # ------------------------------------------------------------------ 创建/查询

    def create_goal(
        self, objective: str, todos: list[dict[str, Any]], *, trace_id: str = ""
    ) -> dict[str, Any]:
        """创建 Goal + todos（≡ create_plan）。返回投影。"""
        goal_id = new_id("goal_")
        self._store.append(
            aggregate_type="Goal",
            aggregate_id=goal_id,
            event_type="GoalCreated",
            payload={"objective": objective, "todos": todos},
            trace_id=trace_id,
        )
        return self.get_goal(goal_id)

    def list_goals(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """最近 Goal 列表（未完成优先）。"""
        goals: list[dict[str, Any]] = []
        for goal_id in self._store.aggregates("Goal"):
            events = self._store.stream(aggregate_type="Goal", aggregate_id=goal_id)
            goal = project_goal_state(events)
            if goal is not None:
                goals.append(goal)
        goals.sort(key=lambda g: (g.get("status") == "completed", g["goal_id"]), reverse=False)
        return goals[:limit]

    def get_goal(self, goal_id: str) -> dict[str, Any]:
        events = self._store.stream(aggregate_type="Goal", aggregate_id=goal_id)
        goal = project_goal_state(events)
        if goal is None:
            raise KeyError(f"Goal {goal_id!r} 不存在")
        return goal

    # ------------------------------------------------------------------ Todo

    def update_todo(
        self, goal_id: str, todo_id: str, status: str, evidence: str = "", *, trace_id: str = ""
    ) -> dict[str, Any]:
        """更新 todo 状态 + 追加证据（≡ update_todo）。"""
        goal = self.get_goal(goal_id)  # 404 校验
        if todo_id not in goal["todos"]:
            raise KeyError(f"todo {todo_id!r} 不存在于 Goal {goal_id!r}")
        status = normalize_status(status)
        self._store.append(
            aggregate_type="Goal",
            aggregate_id=goal_id,
            event_type="TodoUpdated",
            payload={"todo_id": todo_id, "status": status},
            trace_id=trace_id,
        )
        if evidence:
            self._store.append(
                aggregate_type="Goal",
                aggregate_id=goal_id,
                event_type="EvidenceAppended",
                payload={"todo_id": todo_id, "evidence": evidence},
                trace_id=trace_id,
            )
        # 全部 done → GoalCompleted
        if self._all_done(goal_id):
            self._store.append(
                aggregate_type="Goal",
                aggregate_id=goal_id,
                event_type="GoalCompleted",
                payload={},
                trace_id=trace_id,
            )
        return self.get_goal(goal_id)

    def _all_done(self, goal_id: str) -> bool:
        goal = self.get_goal(goal_id)
        todos = (goal.get("todos") or {}).values()
        return bool(todos) and all(t["status"] == "done" for t in todos)

    def claim(
        self,
        goal_id: str,
        todo_id: str,
        *,
        claimant: str = "assistant",
        lease_min: int = DEFAULT_LEASE_MIN,
        trace_id: str = "",
    ) -> dict[str, Any]:
        """claim + lease；已有未过期 claim → 拒绝（fail-closed）。"""
        goal = self.get_goal(goal_id)
        todo = goal["todos"].get(todo_id)
        if todo is None:
            raise KeyError(f"todo {todo_id!r} 不存在")
        if todo["status"] == "done":
            raise RuntimeError(f"todo {todo_id!r} 已完成, 不可 claim")
        existing = todo.get("claim")
        if existing:
            until = existing.get("lease_until", "")
            if until:
                try:
                    if datetime.fromisoformat(until).timestamp() > _now_ts():
                        raise RuntimeError(
                            f"todo {todo_id!r} 已被 {existing.get('claimant')} claim, lease 未过期"
                        )
                except ValueError:
                    pass
        lease_min = max(1, min(int(lease_min), MAX_LEASE_MIN))
        self._store.append(
            aggregate_type="Goal",
            aggregate_id=goal_id,
            event_type="TodoClaimed",
            payload={
                "todo_id": todo_id,
                "claimant": claimant,
                "lease_until": _lease_until(lease_min),
            },
            trace_id=trace_id,
        )
        return self.get_goal(goal_id)

    def release(self, goal_id: str, todo_id: str, *, trace_id: str = "") -> dict[str, Any]:
        self.get_goal(goal_id)
        self._store.append(
            aggregate_type="Goal",
            aggregate_id=goal_id,
            event_type="TodoReleased",
            payload={"todo_id": todo_id},
            trace_id=trace_id,
        )
        return self.get_goal(goal_id)

    # ------------------------------------------------------------------ Quota / Gate / Terminal

    def should_run(self, goal_id: str) -> dict[str, Any]:
        """该不该动（≡ quota_should_run）：未完成 todo + 无依赖阻塞 + 预算内。"""
        goal = self.get_goal(goal_id)
        todos = goal["todos"]
        actionable = [
            t
            for t in todos.values()
            if t["status"] not in ("done",) and not self._blocked(t, todos)
        ]
        spent = int(goal.get("quota_spent", 0))
        budget = int(goal.get("quota_budget", self._default_quota))
        decision = bool(actionable) and spent < budget
        return {
            "goal_id": goal_id,
            "should_run": decision,
            "actionable": len(actionable),
            "spent": spent,
            "budget": budget,
            "reason": ("有可执行 todo 且预算未耗尽" if decision else "无可执行 todo 或预算耗尽"),
        }

    def gate_check(self, goal_id: str, gate_scope: str) -> dict[str, Any]:
        """scoped 决策检查（≡ gate_check）：返回未满足项（不冻结全局）。"""
        goal = self.get_goal(goal_id)
        gates = goal.get("gates") or {}
        status = gates.get(gate_scope, "required")
        unmet: list[str] = []
        if status != "resolved":
            # 依赖未完成的 todo 视为未满足的 gate
            unmet = [t["id"] for t in goal["todos"].values() if t["status"] != "done"]
        return {
            "goal_id": goal_id,
            "gate_scope": gate_scope,
            "resolved": status == "resolved" and not unmet,
            "unmet": unmet,
        }

    def terminal_check(self, action: str, goal_id: str = "") -> dict[str, Any]:
        """terminal 动作只返回「需审批」建议，绝不自动执行（≡ terminal_gate_check）。"""
        return {
            "action": action,
            "goal_id": goal_id,
            "recommendation": "needs_approval",
            "reason": "terminal 动作（发布/删除/重置）一律需人工审批",
            "auto_execute": False,
        }

    def spend(
        self, goal_id: str, todo_id: str, slots: int = 1, *, trace_id: str = ""
    ) -> dict[str, Any]:
        """quota 记账（≡ quota_spend_slot）；超出预算拒绝。"""
        goal = self.get_goal(goal_id)
        if todo_id not in goal["todos"]:
            raise KeyError(f"todo {todo_id!r} 不存在")
        budget = int(goal.get("quota_budget", self._default_quota))
        spent = int(goal.get("quota_spent", 0))
        if spent + slots > budget:
            raise RuntimeError(f"quota 超支: spent={spent} slots={slots} budget={budget}")
        self._store.append(
            aggregate_type="Goal",
            aggregate_id=goal_id,
            event_type="QuotaSpent",
            payload={"todo_id": todo_id, "slots": int(slots)},
            trace_id=trace_id,
        )
        return self.get_goal(goal_id)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _blocked(todo: dict[str, Any], todos: dict[str, dict[str, Any]]) -> bool:
        return any(
            dep in todos and todos[dep]["status"] != "done" for dep in todo.get("depends_on") or []
        )


__all__ = ["GoalService"]
