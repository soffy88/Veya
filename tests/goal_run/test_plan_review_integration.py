"""goal_run × 计划审查接线证明: reject 真的把 state 停在 awaiting_user, approve 放行。

G0 Understand 在离线/无真实 LLM 的测试环境里经常判 decision="ask", 会让
project_run_goal 在 G1 之前就短路返回——所以这里直接测 _run_plan_review_gate
本身(跟 test_runner_review_integration.py 测 _run_dual_axis_review 的思路
一致), 而不是驱动整条 project_run_goal 管线。
"""

from __future__ import annotations

import pytest

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode
from server.goal_run.runner import _run_plan_review_gate


def _state_with_one_task(tmp_path) -> GoalRunState:
    state = GoalRunState(goal_id="g1", goal_text="实现一个简单的 TODO 列表功能")
    state.tasks["T1"] = TaskNode(
        id="T1",
        title="加个模型",
        instruction="加个 Todo 模型",
        acceptance=["模型存在"],
        depends_on=[],
        assignee="hicode",
    )
    return state


@pytest.mark.asyncio
async def test_rejected_plan_blocks_and_sets_awaiting_user(tmp_path, monkeypatch):
    state = _state_with_one_task(tmp_path)

    async def always_reject(**kwargs):
        return {
            "feasibility": {"verdict": "reject", "concerns": ["缺少认证步骤"], "reasoning": "x"},
            "safety": {"verdict": "approve", "concerns": [], "reasoning": "y"},
            "blocked": True,
        }

    monkeypatch.setattr("server.goal_run.plan_review.dual_axis_plan_review", always_reject)

    response = await _run_plan_review_gate(state, state.goal_text, str(tmp_path))

    assert response is not None
    assert response.status == GoalStatus.awaiting_user
    assert response.phase == "plan_review_blocked"
    assert "认证" in (response.block_reason or "")
    assert state.status == GoalStatus.awaiting_user
    assert state.plan_review["blocked"] is True


@pytest.mark.asyncio
async def test_approved_plan_returns_none_and_leaves_status(tmp_path, monkeypatch):
    state = _state_with_one_task(tmp_path)
    state.status = GoalStatus.planning
    calls = []

    async def always_approve(**kwargs):
        calls.append(1)
        approve = {"verdict": "approve", "concerns": [], "reasoning": "ok"}
        return {"feasibility": approve, "safety": approve, "blocked": False}

    monkeypatch.setattr("server.goal_run.plan_review.dual_axis_plan_review", always_approve)

    response = await _run_plan_review_gate(state, state.goal_text, str(tmp_path))

    assert calls, "plan review 应该被调用过"
    assert response is None  # 放行: 不打断 runner 主循环
    assert state.status == GoalStatus.planning  # 门禁本身不改状态, 放行就是 no-op
    assert state.plan_review["blocked"] is False


@pytest.mark.asyncio
async def test_plan_review_disabled_by_env_skips_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")
    state = _state_with_one_task(tmp_path)
    calls = []

    async def should_not_be_called(**kwargs):
        calls.append(1)
        return {"feasibility": {}, "safety": {}, "blocked": True}

    monkeypatch.setattr("server.goal_run.plan_review.dual_axis_plan_review", should_not_be_called)

    response = await _run_plan_review_gate(state, state.goal_text, str(tmp_path))

    assert calls == []
    assert response is None
    assert state.plan_review == {"skipped": True}


@pytest.mark.asyncio
async def test_plan_review_exception_fails_open(tmp_path, monkeypatch):
    state = _state_with_one_task(tmp_path)
    state.status = GoalStatus.planning

    async def boom(**kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr("server.goal_run.plan_review.dual_axis_plan_review", boom)

    response = await _run_plan_review_gate(state, state.goal_text, str(tmp_path))

    assert response is None
    assert state.status == GoalStatus.planning  # 没被拦, 状态未改成 awaiting_user
