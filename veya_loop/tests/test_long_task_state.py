"""veya_loop 长程状态内核行为测试矩阵。

覆盖 (规格: LoopX 六机制内化验收):
  1. 事件重放/恢复: 跨实例重建一致 (连续两天的根基);
  2. 并发: 多进程 append (flock) 无丢失、seq 单调;
  3. 配额: 超支暂停 → 充值恢复 → 事件流留痕;
  4. 交接: handoff 记录 + 重建后可读;
  5. kill -9 崩溃安全: 两轮"写→测→修"后进程被强杀 (os._exit(9)),
     事件流不损坏、第三轮续跑成功 (fsync 关键事件);
  6. 顶层惰性导出: veya_loop.GoalKernel 等公共符号可用。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from veya_loop.obase.loop_event_store import (
    AppendOnlyEventStore,
    QuotaTracker,
    VerifyResult,
)
from veya_loop.omodul.long_task_state import (
    Goal,
    GoalKernel,
)

from veya_loop import (
    AppendOnlyEventStore as TopAppendOnlyEventStore,
)
from veya_loop import (
    GoalKernel as TopGoalKernel,
)
from veya_loop import (
    QuotaTracker as TopQuotaTracker,
)

# ---------------------------------------------------------------------------
# 1. 事件重放 / 跨实例恢复
# ---------------------------------------------------------------------------


async def test_replay_rebuild_across_instances(tmp_path):
    store = AppendOnlyEventStore(tmp_path / "g.jsonl")
    kernel = GoalKernel(store, goal_id="g1")
    await kernel.add_goal("重构结算模块", budget_usd=5.0)
    await kernel.update_todo("t1", title="拆服务", status="done")
    await kernel.update_todo("t2", title="写单测")
    await kernel.require_gate("operator", waiting_on=["t1", "t2"], gate_id="gate-a")
    await kernel.record_handoff("build", "t1 完成")

    fresh = GoalKernel(store, goal_id="g1").rebuild()
    assert isinstance(fresh.goal, Goal)
    assert fresh.goal.title == "重构结算模块"
    assert fresh.goal.todos["t1"].status == "done"
    assert fresh.goal.todos["t2"].status == "open"
    assert fresh.goal.gates["gate-a"].status == "open"
    assert fresh.goal.handoffs[0].to == "build"
    assert fresh.check_integrity().ok
    assert fresh.next_action().id == "t2"  # 续跑决策


async def test_top_level_lazy_exports_are_same_objects(tmp_path):
    """顶层惰性导出 (veya_loop.GoalKernel) 与装配面转发是同一对象。"""
    assert TopAppendOnlyEventStore is AppendOnlyEventStore
    assert TopGoalKernel is GoalKernel
    assert TopQuotaTracker is QuotaTracker


# ---------------------------------------------------------------------------
# 2. 并发 (多进程 flock)
# ---------------------------------------------------------------------------


def _worker(path: str, worker_id: int, n: int) -> int:
    import asyncio

    from veya_loop.obase.loop_event_store import AppendOnlyEventStore

    async def run() -> int:
        store = AppendOnlyEventStore(path)
        for i in range(n):
            await store.append("todo_updated", {"worker": worker_id, "i": i}, fsync=True)
        return store.count()

    return asyncio.run(run())


def test_concurrent_append_multiprocess(tmp_path):
    import multiprocessing as mp

    path = tmp_path / "conc.jsonl"
    with mp.Pool(2) as pool:
        results = pool.starmap(_worker, [(str(path), 0, 20), (str(path), 1, 20)])
    store = AppendOnlyEventStore(path)
    assert all(r >= 20 for r in results)
    assert store.count() == 40
    result: VerifyResult = store.verify()
    assert result.ok, result.error
    assert result.count == 40
    seqs = [e["seq"] for e in store.replay()]
    assert seqs == list(range(1, 41))


# ---------------------------------------------------------------------------
# 3. 配额暂停/恢复 (经 veya_loop 转发)
# ---------------------------------------------------------------------------


async def test_quota_pause_resume_events(tmp_path):
    store = AppendOnlyEventStore(tmp_path / "q.jsonl")
    quota = QuotaTracker(budget_usd=1.0, goal_id="g1", store=store)
    await quota.record_usage(0.6)
    with pytest.raises(Exception) as exc:
        await quota.record_usage(0.5)  # 1.1 > 1.0 → BudgetExceeded
    assert "budget" in str(exc.value).lower()
    assert quota.paused
    await quota.resume(new_budget=2.0)
    await quota.record_usage(0.1)
    types = [e["type"] for e in store.replay()]
    assert types == [
        "quota_consumed",
        "quota_consumed",
        "quota_paused",
        "quota_resumed",
        "quota_consumed",
    ]
    assert store.verify().ok


# ---------------------------------------------------------------------------
# 4. 交接
# ---------------------------------------------------------------------------


async def test_handoff_recorded_and_rebuilt(tmp_path):
    store = AppendOnlyEventStore(tmp_path / "h.jsonl")
    kernel = GoalKernel(store, goal_id="g1")
    await kernel.add_goal("g")
    await kernel.update_todo("t1", status="done")
    await kernel.record_handoff("build", "t1 完成, 下一步拆服务")
    fresh = GoalKernel(store, goal_id="g1").rebuild()
    assert fresh.goal.handoffs[0].to == "build"
    assert fresh.goal.handoffs[0].ts > 0


# ---------------------------------------------------------------------------
# 5. kill -9 崩溃安全: 两轮写→测→修 + 第三轮续跑
# ---------------------------------------------------------------------------

_KILL9_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import os
    import sys

    from veya_loop.obase.loop_event_store import AppendOnlyEventStore
    from veya_loop.omodul.long_task_state import GoalKernel

    async def main() -> None:
        store = AppendOnlyEventStore(sys.argv[1])
        kernel = GoalKernel(store, goal_id="g-kill9")
        await kernel.add_goal("两轮写测修", budget_usd=10.0)
        # round 1: 写 → 测
        await kernel.update_todo("t1", title="实现 A", status="done")
        await kernel.append_evidence("test_pass", {"round": 1, "n": 5}, todo_id="t1")
        # round 2: 写 → 测
        await kernel.update_todo("t2", title="实现 B", status="done")
        await kernel.append_evidence("test_pass", {"round": 2, "n": 8}, todo_id="t2")
        # 进程被强杀 (模拟 kill -9 / 断电), 不走任何清理路径
        os._exit(9)

    asyncio.run(main())
    """
)


async def test_kill9_crash_safe_then_third_round(tmp_path):
    path = tmp_path / "kill9.jsonl"
    script = tmp_path / "kill9_worker.py"
    script.write_text(_KILL9_SCRIPT, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(script), str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 9  # os._exit(9) → 模拟 kill -9

    # 崩溃后: 事件流不损坏 (fsync 关键事件), 重建状态完整
    store = AppendOnlyEventStore(path)
    result: VerifyResult = store.verify()
    assert result.ok, result.error
    assert result.count == 5  # goal_added + 2×todo_updated + 2×evidence_appended

    kernel = GoalKernel(store, goal_id="g-kill9").rebuild()
    assert kernel.goal.todos["t1"].status == "done"
    assert kernel.goal.todos["t2"].status == "done"
    assert len(kernel.goal.evidence) == 2

    # 第三轮续跑 (跨进程恢复后继续写 → 测 → 修)
    await kernel.update_todo("t3", title="实现 C")
    await kernel.append_evidence("test_pass", {"round": 3, "n": 12}, todo_id="t3")
    await kernel.update_todo("t3", status="done")
    assert kernel.check_integrity().ok
    assert kernel.is_complete()
    assert store.count() == 8

    # 再次跨实例重建: 第三轮的成果也持久化
    final = GoalKernel(store, goal_id="g-kill9").rebuild()
    assert final.goal.todos["t3"].status == "done"
    assert final.is_complete()
