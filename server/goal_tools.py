"""server.goal_tools — MasterAgent 工具面：把 GoalKernel 长程任务治理接进主链。

``goal_start`` 是唯一入口：调用后把新建的 goal_id 跟当前 session 关联存起来
(server/goal_session_map.py)，之后 coordinator_master.chat_stream 每一轮都
会自动从这个关联查到 goal_id，构造一个 LongTaskDriver 传给 master_agent 的
pre_round/post_round 钩子——预算追踪、todo 进度提示从下一轮开始自动生效。

不调 goal_start 的会话完全不受影响：goal_id 查不到时
coordinator_master._default_long_task_factory 返回 None，等价于没接线。

预算不需要每轮重新传：ensure_goal 时把 budget_usd 写进事件流，
LongTaskDriver.pre_round/post_round 每轮都会从投影 (QuotaView) 自愈实际预算，
构造 driver 时传不传 budget_usd 都不影响已经开过的 goal。

同步/异步说明：这三个函数都是 async def —— GoalKernel 的写 API
(add_goal/update_todo) 本身是 async (事件落盘)，MasterToolRegistry 对
async 函数直接 await，不需要走线程池。
"""

from __future__ import annotations

import uuid

from oservi.long_task_driver import open_long_task

from server.goal_session_map import GOAL_LOOPS_DIR, get_goal_id, set_goal_id


async def goal_start(title: str, budget_usd: float = 5.0) -> str:
    """开一个长程任务目标：之后每轮自动做预算追踪 (超支本轮直接暂停不再调用 LLM)。

    适合会连续跑很多轮、需要控制花费的任务；普通对话不用调这个，调了也不会
    影响其他没开目标的会话。
    """
    from server.tool_registry import _current_master_session  # lazy: 避免与 tool_registry 循环导入

    session_id = _current_master_session.get()
    if not session_id:
        return "goal_start: 当前不在一个已知 session 里，无法关联"
    goal_id = uuid.uuid4().hex[:12]
    driver = open_long_task(GOAL_LOOPS_DIR, goal_id=goal_id, budget_usd=budget_usd)
    await driver.ensure_goal(title)
    set_goal_id(session_id, goal_id)
    return f"✅ 已开长程任务 goal_id={goal_id}，预算 ${budget_usd:g}。本 session 后续轮次自动追踪进度和花费。"


async def goal_add_todo(todo_id: str, title: str, blocked_by: list[str] | None = None) -> str:
    """给当前 session 关联的长程任务加一个 todo (下一轮的 prompt 提示会指向它)。"""
    from server.tool_registry import _current_master_session

    session_id = _current_master_session.get()
    goal_id = get_goal_id(session_id) if session_id else None
    if not goal_id:
        return "goal_add_todo: 当前 session 还没开长程任务，先调 goal_start"
    driver = open_long_task(GOAL_LOOPS_DIR, goal_id=goal_id)
    driver.kernel.rebuild()  # 载入最新投影再写，避免覆盖别的并发写入
    await driver.kernel.update_todo(todo_id, title=title, blocked_by=blocked_by or [])
    return f"✅ 已加 todo {todo_id}: {title}"


async def goal_status(
    todo_id: str | None = None, status: str | None = None, note: str | None = None
) -> str:
    """看当前 session 关联的长程任务进度 (todo/gate/预算)；传 todo_id+status 顺便更新一个 todo 状态。"""
    from server.tool_registry import _current_master_session

    session_id = _current_master_session.get()
    goal_id = get_goal_id(session_id) if session_id else None
    if not goal_id:
        return "goal_status: 当前 session 还没开长程任务"
    driver = open_long_task(GOAL_LOOPS_DIR, goal_id=goal_id)
    driver.kernel.rebuild()
    if todo_id is not None and status is not None:
        await driver.kernel.update_todo(todo_id, status=status, note=note)
        driver.kernel.rebuild()
    goal = driver.kernel.goal
    if goal is None:
        return f"goal_status: goal_id={goal_id} 事件流为空 (异常状态)"
    open_todos = driver.kernel.open_todos()
    done = [t for t in goal.todos.values() if t.status == "done"]
    gates = driver.kernel.pending_gates()
    # goal.quota 是从事件流直接投影出来的 QuotaView —— 用它而不是
    # driver.quota (运行时 QuotaTracker), 后者只有走过 pre_round/post_round
    # 才会被同步, 这里只 rebuild 了 kernel, 不该假设 driver.quota 是最新的。
    q = goal.quota
    budget = q.budget_usd if q.budget_usd is not None else 0.0
    lines = [
        f"目标: {goal.title} (goal_id={goal_id})",
        f"todo: {len(done)}/{len(goal.todos)} done, {len(open_todos)} open",
        f"gate: {len(gates)} pending",
        f"预算: 已花 ${q.spent_usd:.4f} / ${budget:.4f} (剩 ${q.remaining_usd:.4f})"
        + ("，已暂停" if q.paused else ""),
    ]
    if open_todos:
        lines.append("open todos: " + ", ".join(f"{t.id}:{t.title}" for t in open_todos[:10]))
    return "\n".join(lines)
