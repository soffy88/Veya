"""loop-plane domain.state — 事件 + 投影（SPEC §3.2 映射）。

旧 plan_todo/state_kernel 双源 → 本服务单一真相源：
    写路径只追加 DomainEvent，读路径投影（project_goal_state）。

事件-旧字段映射:
    plan_id            → Goal aggregate_id
    objective          → GoalCreated.payload.objective
    todos[].status     → TodoUpdated.payload.status
    todos[].evidence   → EvidenceAppended.payload.evidence
    claim / lease      → TodoClaimed.payload {claimant, lease_until}
    spends             → QuotaSpent.payload {slots}
    gates              → GateRequired / GateResolved
"""

from __future__ import annotations

from typing import Any

TODO_STATUSES = ("open", "in_progress", "done", "blocked")

# 旧 plan_todo 状态 → 新模型状态
_LEGACY_STATUS_MAP = {
    "open": "open",
    "in_progress": "in_progress",
    "done": "done",
    "blocked": "blocked",
    "completed": "done",
    "running": "in_progress",
    "pending": "open",
}


def normalize_status(status: str) -> str:
    """兼容旧工具传入的状态值（completed/running/pending）。"""
    out = _LEGACY_STATUS_MAP.get(status.lower())
    if out is None:
        raise ValueError(f"非法 status {status!r} (允许: {TODO_STATUSES})")
    return out


def project_goal_state(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从事件流投影 Goal 状态（纯函数）。

    返回 {goal_id, title, objective, status, todos, gates, quota_spent,
          trace_ids, render_text}；无 GoalCreated → None。
    """
    created: dict[str, Any] | None = None
    todos: dict[str, dict[str, Any]] = {}
    gates: dict[str, str] = {}
    quota_spent = 0
    trace_ids: list[str] = []

    for event in events:
        payload = event.get("payload") or {}
        etype = event.get("event_type")
        trace = event.get("trace_id") or ""
        if trace and trace not in trace_ids:
            trace_ids.append(trace)

        if etype == "GoalCreated":
            created = {
                "goal_id": event["aggregate_id"],
                "title": payload.get("objective", ""),
                "objective": payload.get("objective", ""),
                "status": "active",
            }
            for todo in payload.get("todos") or []:
                tid = todo.get("id")
                todos[tid] = {
                    "id": tid,
                    "title": todo.get("title", ""),
                    "status": normalize_status(todo.get("status", "open")),
                    "depends_on": list(todo.get("depends_on") or []),
                    "evidence": [],
                    "claim": None,
                    "spends": 0,
                }
        elif etype == "TodoUpdated" and created is not None:
            todo = todos.get(payload.get("todo_id"))
            if todo:
                todo["status"] = normalize_status(payload.get("status", "open"))
        elif etype == "EvidenceAppended" and created is not None:
            todo = todos.get(payload.get("todo_id"))
            if todo:
                todo["evidence"].append(payload.get("evidence", ""))
        elif etype == "TodoClaimed" and created is not None:
            todo = todos.get(payload.get("todo_id"))
            if todo:
                todo["claim"] = {
                    "claimant": payload.get("claimant", ""),
                    "lease_until": payload.get("lease_until", ""),
                }
        elif etype == "TodoReleased" and created is not None:
            todo = todos.get(payload.get("todo_id"))
            if todo:
                todo["claim"] = None
        elif etype == "GateRequired" and created is not None:
            gates[payload.get("gate_scope", "")] = "required"
        elif etype == "GateResolved" and created is not None:
            gates[payload.get("gate_scope", "")] = "resolved"
        elif etype == "QuotaSpent" and created is not None:
            quota_spent += int(payload.get("slots", 0))
        elif etype == "GoalCompleted" and created is not None:
            created["status"] = "completed"

    if created is None:
        return None

    created["todos"] = todos
    created["gates"] = gates
    created["quota_spent"] = quota_spent
    created["trace_ids"] = trace_ids
    created["render_text"] = render_text(created)
    return created


def render_text(goal: dict[str, Any], *, brief: bool = False) -> str:
    """兼容旧 plan_status 的文本视图（工具友好，无感替换）。"""
    lines: list[str] = []
    status = goal.get("status", "active")
    lines.append(f"Goal: {goal.get('objective', '')} [id={goal.get('goal_id', '')}] [{status}]")
    for todo in (goal.get("todos") or {}).values():
        dep = f" (depends: {', '.join(todo['depends_on'])})" if todo.get("depends_on") else ""
        evidence = f" evidence: {todo['evidence'][-1]}" if todo.get("evidence") else ""
        claim = f" claim: {todo['claim']['claimant']}" if todo.get("claim") else ""
        lines.append(f"  - [{todo['status']}] {todo['title']} (id={todo['id']}){dep}{claim}{evidence}")
    if not brief:
        gates = goal.get("gates") or {}
        if gates:
            lines.append(f"Gates: {gates}")
        lines.append(f"Quota spent: {goal.get('quota_spent', 0)}")
    return "\n".join(lines)


__all__ = ["TODO_STATUSES", "normalize_status", "project_goal_state", "render_text"]
