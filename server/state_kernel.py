"""server.state_kernel — 状态内核 (ARCHITECTURE_STATE_KERNEL.md Phase 1 补全)。

主脑设置零改动 (冻结架构): 只新增能力工具, 模型自主调用。
复用 plan_todo 的 plan JSON 为单一真相源 (不重造状态), 补上其缺失的
控制面对象:

- system_quota_should_run : 该不该动 (deliver/wait/repair/ask/replan)
- system_todo_claim       : 谁接手 (claim + 可回收 lease, TTL)
- system_gate_check       : 哪个 scoped 决策未满足 (不冻结全局)

与 plan_todo (create_plan/plan_status/update_todo + evidence) 组合成
完整状态内核: Goal(create_plan) / Todo(update_todo) / Claim-Lease /
Gate / Evidence(update_todo evidence) / Quota(quota_should_run)。
"""

from __future__ import annotations

import time
from typing import Any

from server.plan_todo import _load as _load_plan

# Claim/Lease TTL (对标 LoopX: lease 显式可选、TTL 内占用、过期 fail-closed)
DEFAULT_LEASE_MIN = 45
MAX_LEASE_MIN = 24 * 60


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _now_ts() -> float:
    return time.time()


def _todos(plan: dict) -> list[dict]:
    return plan.get("todos", [])


def _todo(plan: dict, todo_id: str) -> dict:
    for t in _todos(plan):
        if t.get("id") == todo_id:
            return t
    raise ValueError(f"todo 不存在: {todo_id} (计划 {plan.get('plan_id')})")


# ── Quota: 该不该动 ──────────────────────────────────────────────────

def _plan_done(plan: dict) -> bool:
    todos = _todos(plan)
    return bool(todos) and all(t.get("status") == "done" for t in todos)


def _blocked_by_dependency(plan: dict, todo: dict) -> list[str]:
    """依赖未完成 → 返回阻塞依赖 todo id 列表 (空 = 依赖满足)。"""
    blocked: list[str] = []
    for dep in todo.get("depends_on", []) or []:
        dep_todo = next((t for t in _todos(plan) if t.get("id") == dep), None)
        if dep_todo is None or dep_todo.get("status") != "done":
            blocked.append(dep)
    return blocked


async def quota_should_run(plan_id: str = "") -> str:
    """控制面判断: 当前该不该动 / 该动什么。

    返回 {should_run, action, reason}:
      action=deliver  → 有可推进的未完成任务 (依赖满足)
      action=repair   → 有 blocked 项 (修复/解除)
      action=wait     → 无活跃可推进工作 (等外部/等指令/全部完成)
    """
    if plan_id:
        try:
            plan = _load_plan(plan_id)
        except Exception as exc:  # noqa: BLE001
            return f"quota: {exc}"
    else:
        # 无 plan_id → 找最近有未完成项的活跃计划
        import glob
        import json
        import os

        from server.plan_todo import _plans_dir

        latest: dict | None = None
        try:
            for f in sorted(glob.glob(os.path.join(str(_plans_dir()), "*.json")),
                            key=os.path.getmtime, reverse=True):
                try:
                    with open(f, encoding="utf-8") as fp:
                        plan = json.load(fp)
                except Exception:
                    continue
                if not _plan_done(plan):
                    latest = plan
                    break
        except Exception:
            pass
        plan = latest
        if plan is None:
            return "{\"should_run\": false, \"action\": \"wait\", \"reason\": \"无活跃计划 (无未完成 todo 的计划); 等待新指令\"}"

    todos = _todos(plan)
    if not todos:
        return "{\"should_run\": false, \"action\": \"wait\", \"reason\": \"计划无 todo; 等待补充或新指令\"}"

    if _plan_done(plan):
        return ("{\"should_run\": false, \"action\": \"wait\", "
                "\"reason\": \"计划已全部完成; 等待新指令或 replan\"}")

    # 可推进: open/in_progress 且依赖满足
    runnable = [t for t in todos if t.get("status") in ("open", "in_progress")
                and not _blocked_by_dependency(plan, t)]
    if runnable:
        ids = ", ".join(t.get("id", "?") for t in runnable[:5])
        return (f"{{\"should_run\": true, \"action\": \"deliver\", "
                f"\"reason\": \"可推进 todo: {ids}\"}}")

    # 全部未完成项都被依赖/blocked 卡住 → repair
    blocked_items = [t for t in todos
                     if t.get("status") == "blocked" or _blocked_by_dependency(plan, t)]
    if blocked_items:
        ids = ", ".join(t.get("id", "?") for t in blocked_items[:5])
        return (f"{{\"should_run\": true, \"action\": \"repair\", "
                f"\"reason\": \"存在 blocked/依赖未满足: {ids}; 需修复或解除\"}}")

    return "{\"should_run\": false, \"action\": \"wait\", \"reason\": \"暂无可推进工作\"}"


# ── Claim / Lease: 谁接手 / 谁占用 ───────────────────────────────────

def _claim_active(todo: dict) -> dict | None:
    """当前有效 claim (未过期) → 返回 claim 信息, 否则 None。"""
    claim = todo.get("claim")
    if not isinstance(claim, dict):
        return None
    lease_until = claim.get("lease_until")
    if lease_until and _now_ts() > float(lease_until):
        return None  # lease 过期 → fail-closed (视为可重新认领)
    return claim


async def todo_claim(plan_id: str, todo_id: str,
                     lease_minutes: int = DEFAULT_LEASE_MIN) -> str:
    """认领一个 todo (claim + 可回收 lease)。

    lease 默认 {DEFAULT_LEASE_MIN} 分钟 (上限 {MAX_LEASE_MIN // 60}h); 过期自动
    释放 (fail-closed)。done/blocked 不能认领; 他人有效 claim 内不能抢占。
    """
    lease_minutes = max(1, min(int(lease_minutes), MAX_LEASE_MIN))
    try:
        plan = _load_plan(plan_id)
    except Exception as exc:  # noqa: BLE001
        return f"claim: {exc}"
    todo = _todo(plan, todo_id)
    if todo.get("status") == "done":
        return f"claim: todo {todo_id} 已完成, 无需认领"
    if todo.get("status") == "blocked":
        return f"claim: todo {todo_id} 处于 blocked, 先修复再认领"

    active = _claim_active(todo)
    if active:
        return (f"claim: todo {todo_id} 已被认领 (claimed_by={active.get('claimed_by', '?')}, "
                f"lease_until={active.get('lease_until', '?')}); 未过期不可抢占")

    lease_until = _now_ts() + lease_minutes * 60
    todo["claim"] = {
        "claimed_by": "master",  # 当前单主脑; 多 peer 时代替为 peer id
        "claimed_at": _now(),
        "lease_until": lease_until,
    }
    todo["status"] = "in_progress"
    from server.plan_todo import _save as _save_plan

    _save_plan(plan)
    until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lease_until))
    return f"✅ 已认领 todo {todo_id} (lease 至 {until}, {lease_minutes} 分钟)"


# ── Gate: 哪个 scoped 决策未满足 (不冻结全局) ────────────────────────

async def gate_check(plan_id: str, gate_scope: str) -> str:
    """检查一个 scoped 决策门 (gate) 是否满足。

    gate_scope: 描述该门约束的待办/产物/条件 (如 "CI 通过" / "依赖 X 完成")。
    返回 {gate_open, scope, blocking_todos}。只检查指定 scope 相关依赖,
    不冻结计划全局 (其他 todo 仍可推进)。
    """
    try:
        plan = _load_plan(plan_id)
    except Exception as exc:  # noqa: BLE001
        return f"gate: {exc}"
    scope = gate_scope.strip()
    # 找到 scope 相关的工作 todo (标题含 scope 或依赖引用含 scope)
    work = [t for t in _todos(plan)
            if scope in str(t.get("title", ""))
            or any(scope in d for d in (t.get("depends_on") or []))]
    # 这些工作项的前置依赖未完成 → gate 未开 (blocking 列前置 todo id)
    blocking: list[str] = []
    for t in work:
        for dep in t.get("depends_on", []) or []:
            dep_t = next((x for x in _todos(plan) if x.get("id") == dep), None)
            if dep_t is None or dep_t.get("status") != "done":
                blocking.append(dep)
    gate_open = not blocking
    import json as _json

    return _json.dumps({"gate_open": gate_open, "scope": scope,
                        "blocking_todos": blocking}, ensure_ascii=False)
