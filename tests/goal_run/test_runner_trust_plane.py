"""runner.py × trust_plane 接线测试(VAOM Trust Plane P1 落地, 见
docs/dev/rfc-01-vaom.md, docs/VEYA_3.0_GAP_AUDIT.md §3.4/§5)。

验证:
1. 任务验收通过 → trust_plane.jsonl 落一份 Claim(verified)+Evidence+EvaluationResult
   +VerifiedState, 且 task.status 依然由 verify_result.passed 驱动(纯旁路, 默认
   不改变现有行为)。
2. 任务验收失败且重试用尽(blocked) → 落一份 Claim(rejected), 无 VerifiedState。
3. VEYA_GOAL_RUN_VERIFIED_GATE=1 时, 若记录失败, 已经验收通过的任务被拦回
   blocked——这是唯一一个真的改变现有行为的开关, 默认关闭时完全不生效。
"""

from __future__ import annotations

import pytest

from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalRunState, TaskNode, TaskStatus
from server.goal_run.runner import _process_one_task
from server.goal_run.trust_plane import read_trust_plane_records
from server.goal_run.verify import VerifyResult


async def _immediate(value):
    return value


def _make_task(*, retries: int = 0) -> TaskNode:
    return TaskNode(
        id="t1",
        title="A",
        instruction="do A",
        acceptance=["file exists"],
        depends_on=[],
        assignee="hicode",
        retries=retries,
    )


def _make_state() -> GoalRunState:
    state = GoalRunState(goal_id="g1", goal_text="test goal")
    return state


@pytest.mark.asyncio
async def test_passed_task_writes_verified_state_by_default(tmp_path, monkeypatch):
    async def _leaf(*_a, **_kw):
        return LeafResult(status="completed", summary="1 passed, 0 failed", artifacts=[])

    async def _verify(*_a, **_kw):
        return VerifyResult(passed=True, summary="1 passed, 0 failed")

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", _leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _verify)
    monkeypatch.setattr(
        "server.goal_run.runner._run_dual_axis_review", lambda *a, **kw: _immediate(None)
    )

    state = _make_state()
    task = _make_task()
    state.tasks[task.id] = task

    result = await _process_one_task(task, state, str(tmp_path))

    assert result is None
    assert task.status == TaskStatus.completed  # 现有行为不变: verify_result.passed 直接驱动

    records = read_trust_plane_records(str(tmp_path), "g1")
    types = [r["_type"] for r in records]
    assert "Claim" in types and "VerifiedState" in types
    claim = next(r for r in records if r["_type"] == "Claim")
    assert claim["status"] == "verified"
    assert claim["task_id"] == "t1"

    from server.goal_run import runner as runner_mod

    profile = runner_mod.performance_store.aggregate("hicode", "goal_run_task")
    assert profile is not None
    assert profile.sample_size == 1
    assert profile.success_rate == 1.0


@pytest.mark.asyncio
async def test_blocked_task_writes_rejected_claim_without_verified_state(tmp_path, monkeypatch):
    async def _leaf(*_a, **_kw):
        return LeafResult(status="completed", summary="0 passed, 1 failed", artifacts=[])

    async def _verify(*_a, **_kw):
        return VerifyResult(passed=False, summary="0 passed, 1 failed", reason="assertion failed")

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", _leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _verify)

    state = _make_state()
    task = _make_task(retries=1)  # max_retries_per_task 默认 2, 再失败一次即用尽
    state.tasks[task.id] = task

    result = await _process_one_task(task, state, str(tmp_path))

    assert result is not None  # 重试用尽触发 apply_block_policy, 非 None 表示 goal 终止
    assert task.status == TaskStatus.blocked

    records = read_trust_plane_records(str(tmp_path), "g1")
    types = [r["_type"] for r in records]
    assert "Claim" in types
    assert "VerifiedState" not in types
    claim = next(r for r in records if r["_type"] == "Claim")
    assert claim["status"] == "rejected"

    from server.goal_run import runner as runner_mod

    profile = runner_mod.performance_store.aggregate("hicode", "goal_run_task")
    assert profile is not None
    assert profile.success_rate == 0.0


@pytest.mark.asyncio
async def test_verified_gate_off_by_default_ignores_recording_failure(
    tmp_path, monkeypatch, request
):
    monkeypatch.delenv("VEYA_GOAL_RUN_VERIFIED_GATE", raising=False)

    async def _leaf(*_a, **_kw):
        return LeafResult(status="completed", summary="ok", artifacts=[])

    async def _verify(*_a, **_kw):
        return VerifyResult(passed=True, summary="ok")

    def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", _leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _verify)
    monkeypatch.setattr(
        "server.goal_run.runner._run_dual_axis_review", lambda *a, **kw: _immediate(None)
    )
    monkeypatch.setattr("server.goal_run.runner.append_trust_plane_records", _boom)

    state = _make_state()
    task = _make_task()
    state.tasks[task.id] = task

    result = await _process_one_task(task, state, str(tmp_path))

    assert result is None
    assert task.status == TaskStatus.completed  # gate 关闭: 记录失败不影响现有行为


@pytest.mark.asyncio
async def test_verified_gate_on_blocks_task_when_recording_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_GOAL_RUN_VERIFIED_GATE", "1")

    async def _leaf(*_a, **_kw):
        return LeafResult(status="completed", summary="ok", artifacts=[])

    async def _verify(*_a, **_kw):
        return VerifyResult(passed=True, summary="ok")

    def _boom(*_a, **_kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", _leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _verify)
    monkeypatch.setattr(
        "server.goal_run.runner._run_dual_axis_review", lambda *a, **kw: _immediate(None)
    )
    monkeypatch.setattr("server.goal_run.runner.append_trust_plane_records", _boom)

    state = _make_state()
    task = _make_task()
    state.tasks[task.id] = task

    result = await _process_one_task(task, state, str(tmp_path))

    # verify 明明 passed, 但 gate 开启且记录失败 → 打回 blocked, 不是 completed
    assert result is not None
    assert task.status == TaskStatus.blocked
    assert "verified_state_write_failed" in (task.block_reason or "")
    assert task.id not in state.completed_ids


@pytest.mark.asyncio
async def test_finalize_episode_extracts_memory_candidate(tmp_path, monkeypatch):
    """_finalize_episode(见 runner.py) 除了写 episode.json, 还调
    memory_controller.extract_candidates(VAOM MemoryController P3, 见
    docs/dev/rfc-01-vaom.md) 从刚完成的 episode 里提炼候选记忆——验证真的接上了,
    不是挂了个没人调的函数。"""

    async def _leaf(*_a, **_kw):
        return LeafResult(status="completed", summary="leaf done", artifacts=[])

    async def _verify(*_a, **_kw):
        # Claim.statement 来自 verify_result.summary(见 runner.py::_record_trust_plane),
        # 不是 leaf 的 summary——搜索词要对齐真实数据流向, 不是 leaf 输出。
        return VerifyResult(passed=True, summary="迁移脚本先跑通再动代码")

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", _leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", _verify)
    monkeypatch.setattr(
        "server.goal_run.runner._run_dual_axis_review", lambda *a, **kw: _immediate(None)
    )

    state = _make_state()
    task = _make_task()
    state.tasks[task.id] = task

    await _process_one_task(task, state, str(tmp_path))

    from server.goal_run import runner as runner_mod

    runner_mod._finalize_episode(state, str(tmp_path), outcome="completed")

    candidates = runner_mod.memory_controller.search("迁移脚本")
    assert len(candidates) == 1
    assert candidates[0].type == "episodic"
    assert candidates[0].provenance == "goal_run:g1"
