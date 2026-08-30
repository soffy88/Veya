"""goal_run × [P] 并行执行接线证明(smart-ralph 内化，见 memory
project_veya_pi_gap_audit)。

用真实耗时的 fake_leaf 证明 [P] 批次是真的 asyncio.gather 并发跑(总耗时接近
单个任务耗时, 不是 N 倍), 非 [P] 任务依然严格串行(总耗时是 N 倍)。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalRunState, TaskNode, TaskStatus
from server.goal_run.runner import _process_one_task
from server.goal_run.verify import VerifyResult

_SLEEP_S = 0.15


async def _slow_leaf(*_args, **_kwargs) -> LeafResult:
    await asyncio.sleep(_SLEEP_S)
    return LeafResult(status="completed", summary="done", artifacts=[])


async def _fake_verify(*_args, **_kwargs) -> VerifyResult:
    return VerifyResult(passed=True, summary="ok")


def _make_state_with_tasks(*, parallel: bool, count: int = 3) -> GoalRunState:
    state = GoalRunState(goal_id="g1", goal_text="test")
    for i in range(count):
        tid = f"t{i}"
        state.tasks[tid] = TaskNode(
            id=tid,
            title=tid,
            instruction=tid,
            acceptance=["x"],
            depends_on=[],
            assignee="hicode",
            parallel=parallel,
        )
    return state


@pytest.mark.asyncio
async def test_parallel_batch_runs_concurrently_not_serially(tmp_path, monkeypatch):
    started = 0
    active = 0
    peak_active = 0
    all_started = asyncio.Event()

    async def concurrent_leaf(*_args, **_kwargs) -> LeafResult:
        nonlocal started, active, peak_active
        started += 1
        active += 1
        peak_active = max(peak_active, active)
        if started == 3:
            all_started.set()
        try:
            # A serial implementation cannot pass this barrier: the first
            # task waits until all three eligible tasks have entered the leaf.
            await asyncio.wait_for(all_started.wait(), timeout=1.0)
        finally:
            active -= 1
        return LeafResult(status="completed", summary="done", artifacts=[])

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", concurrent_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _fake_verify)
    monkeypatch.setattr(
        "server.goal_run.runner._run_dual_axis_review", lambda *a, **kw: _immediate(None)
    )
    state = _make_state_with_tasks(parallel=True, count=3)

    results = await asyncio.wait_for(
        asyncio.gather(*(_process_one_task(t, state, str(tmp_path)) for t in state.tasks.values())),
        timeout=2.0,
    )

    assert all(r is None for r in results)
    assert all(t.status == TaskStatus.completed for t in state.tasks.values())
    assert started == 3
    assert peak_active == 3


@pytest.mark.asyncio
async def test_non_parallel_tasks_run_strictly_serial_via_process_one_task(tmp_path, monkeypatch):
    """对照组: 非 [P] 任务哪怕手动一个个 await, 耗时应该接近 N 倍(证明没有
    偷偷并发, sleep 是真实累加的)。"""
    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", _slow_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _fake_verify)
    monkeypatch.setattr(
        "server.goal_run.runner._run_dual_axis_review", lambda *a, **kw: _immediate(None)
    )
    state = _make_state_with_tasks(parallel=False, count=3)

    start = time.monotonic()
    for t in state.tasks.values():
        await _process_one_task(t, state, str(tmp_path))
    elapsed = time.monotonic() - start

    assert elapsed >= _SLEEP_S * 2.5


async def _immediate(value):
    return value
