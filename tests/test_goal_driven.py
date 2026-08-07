"""Goal-Driven 长程编排门禁 — while 循环 / gate 自动验证 / 心跳失活。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))

from omodul.long_task_goal import GATE_KIND_AUTO  # noqa: E402
from oservi.goal_driven_loop import GoalDrivenLoop  # noqa: E402
from oservi.long_task_driver import open_long_task  # noqa: E402


@pytest.fixture
async def loop_driver(tmp_path):
    """预置 goal + auto gate 的 driver。"""
    driver = open_long_task(tmp_path, goal_id="g1", budget_usd=5.0)
    await driver.ensure_goal("写一个加法函数", meta={"lang": "py"})
    kernel = driver.kernel
    await kernel.update_todo("t1", title="实现 add(a,b)")
    await kernel.update_todo("t2", title="写测试")
    await kernel.require_gate("tests_pass", kind=GATE_KIND_AUTO, waiting_on=[])
    return driver


def _engine_ok(driver, rounds: list[int]):
    """引擎执行: 每轮完成一个 todo + 附证据。"""
    async def engine(prompt_suffix):
        kernel = driver.kernel
        goal = kernel.goal
        todo = goal.next_action() if goal else None
        if todo:
            await kernel.update_todo(todo.id, status="done", note="引擎完成")
            await kernel.append_evidence("code", {"ok": True}, todo_id=todo.id)
        return {"ok": True, "output": f"round {len(rounds)}", "cost_usd": 0.01,
                "rounds": len(rounds)}

    return engine


@pytest.mark.asyncio
async def test_goal_driven_loop_completes(tmp_path):
    """while 循环: 全部 todo 完成 + gate 验证通过 → completed。"""
    driver = open_long_task(tmp_path, goal_id="g2", budget_usd=5.0)

    async def _setup():
        await driver.ensure_goal("加法函数")
        k = driver.kernel
        await k.update_todo("t1", title="实现")
        await k.require_gate(GATE_KIND_AUTO, [], gate_id="ok")

    async def _engine(prompt):
        k = driver.kernel
        todo = k.goal.next_action() if k.goal else None
        if todo:
            await k.update_todo(todo.id, status="done", note="done")
        return {"ok": True, "output": "x", "cost_usd": 0.01}

    async def _verifier(kernel, todo_id):
        return True, "产出验证通过"

    loop = GoalDrivenLoop(driver, verifier=_verifier, max_rounds=10)
    await _setup()
    report = await loop.run(_engine)
    assert report.completed is True
    assert report.status == "completed"
    assert report.rounds >= 1
    assert report.gates_resolved >= 1


@pytest.mark.asyncio
async def test_goal_driven_loop_gate_rejection_continues(tmp_path):
    """gate 未通过 → 循环继续 (不达标不停机)。"""
    driver = open_long_task(tmp_path, goal_id="g3", budget_usd=5.0)

    async def _setup():
        await driver.ensure_goal("重写函数")
        k = driver.kernel
        await k.update_todo("t1", title="实现 v2")
        await k.require_gate(GATE_KIND_AUTO, [], gate_id="perf")

    calls = {"n": 0}

    async def _engine(prompt):
        k = driver.kernel
        todo = k.goal.next_action() if k.goal else None
        if todo:
            await k.update_todo(todo.id, status="done", note="done")
        calls["n"] += 1
        return {"ok": True, "output": "x", "cost_usd": 0.01}

    async def _verifier(kernel, todo_id):
        return False, "性能不达标"      # 产出验证一直不通过 → todo 重开

    loop = GoalDrivenLoop(driver, verifier=_verifier, max_rounds=4)
    await _setup()
    report = await loop.run(_engine)
    assert report.completed is False
    assert report.status == "max_rounds"      # 护栏触发
    assert report.gates_resolved == 0          # 验证全拒绝 (无通过)
    assert calls["n"] == 4                    # 循环持续到轮数上限


@pytest.mark.asyncio
async def test_goal_driven_loop_quota_pause(tmp_path):
    """预算超支 → quota_paused (QuotaTracker 联动)。"""
    driver = open_long_task(tmp_path, goal_id="g4", budget_usd=0.01)

    async def _setup():
        await driver.ensure_goal("任务")
        k = driver.kernel
        await k.update_todo("t1", title="t1")

    async def _engine(prompt):
        return {"ok": True, "output": "x", "cost_usd": 0.02}   # 单轮超预算

    loop = GoalDrivenLoop(driver, max_rounds=5)
    await _setup()
    report = await loop.run(_engine)
    assert report.completed is False
    assert report.status == "quota_paused"


@pytest.mark.asyncio
async def test_goal_driven_loop_heartbeat_and_stall(tmp_path):
    """心跳: run 期间更新; 失活检测 (外部驱动模式)。"""
    driver = open_long_task(tmp_path, goal_id="g5", budget_usd=5.0)
    beats: list[str] = []

    loop = GoalDrivenLoop(driver, on_heartbeat=lambda gid: beats.append(gid),
                          heartbeat_timeout_s=60)

    async def _setup():
        await driver.ensure_goal("心跳")
        k = driver.kernel
        await k.update_todo("t", title="t")
        await k.require_gate(GATE_KIND_AUTO, [], gate_id="g")

    async def _engine(prompt):
        return {"ok": True, "output": "x", "cost_usd": 0.0}

    async def _verifier(kernel, todo_id):
        return True, "ok"

    await _setup()
    await loop._heartbeat()
    assert beats == ["g5"]
    assert loop.stalled() is False
    # 模拟失活: 心跳时间拨回
    loop.stats.last_heartbeat -= 120
    assert loop.stalled() is True
    prompt = await loop.restart_prompt()
    assert "长程任务自动续跑" in prompt
    assert loop.stats.restarts == 1


@pytest.mark.asyncio
async def test_goal_driven_api_status(tmp_path, monkeypatch):
    """API: goal 投影状态查询。"""


    # 建一个 goal 供查询
    driver = open_long_task(tmp_path, goal_id="g_api", budget_usd=5.0)

    async def _setup():
        await driver.ensure_goal("API goal")
        k = driver.kernel
        await k.update_todo("t1", title="t1")

    await _setup()

    # 跨进程投影查询 (等价 API status 逻辑)
    from obase.loop_event_store import AppendOnlyEventStore
    from omodul.long_task_goal import GoalKernel

    kernel = GoalKernel(
        AppendOnlyEventStore(str(tmp_path / "g_api.jsonl")), goal_id="g_api").rebuild()
    assert kernel.goal is not None
    assert kernel.goal.title == "API goal"
    assert len(kernel.goal.open_todos()) == 1
    assert kernel.goal.is_complete() is False


def test_ledger_registered():
    from server.operator_ledger import goal_driven_ledger_summary

    s = goal_driven_ledger_summary()
    assert s[0]["name"] == "goal_driven_loop"
    assert s[0]["status"] == "registered"
