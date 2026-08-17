"""Spec Kit constitution: persist + runner red-line."""

from __future__ import annotations

import pytest
from obase.loop_breaker import init_breaker, reset_breaker

from server.goal_run.models import GoalRunState, GoalStatus, TaskNode, TaskStatus
from server.goal_run.runner import _constitution_guard


def test_constitution_roundtrip():
    state = GoalRunState(
        goal_id="g1",
        goal_text="setup",
        constitution="Do not use axios\n",
        status=GoalStatus.running,
    )
    data = state.to_taskgraph_json()
    restored = GoalRunState.from_taskgraph_json(data, "setup")
    assert "axios" in restored.constitution


@pytest.mark.asyncio
async def test_constitution_guard_intervened(tmp_path):
    state = GoalRunState(
        goal_id="g1",
        goal_text="setup",
        constitution="Do not use axios\n",
        status=GoalStatus.running,
    )
    task = TaskNode(
        id="T1",
        title="client",
        instruction="write client",
        acceptance=["ok"],
        depends_on=[],
        assignee="hicode",
    )
    state.tasks["T1"] = task
    token = init_breaker()
    try:
        rec = await _constitution_guard(state, task, "npm i axios", str(tmp_path))
    finally:
        reset_breaker(token)
    assert rec is not None
    assert rec.status == GoalStatus.blocked
    assert task.status == TaskStatus.blocked
    assert "宪法" in (task.block_reason or "")


@pytest.mark.asyncio
async def test_constitution_guard_clean(tmp_path):
    state = GoalRunState(
        goal_id="g1",
        goal_text="setup",
        constitution="Do not use axios\n",
        status=GoalStatus.running,
    )
    task = TaskNode(
        id="T1",
        title="client",
        instruction="write client",
        acceptance=["ok"],
        depends_on=[],
        assignee="hicode",
    )
    token = init_breaker()
    try:
        rec = await _constitution_guard(state, task, "used fetch", str(tmp_path))
    finally:
        reset_breaker(token)
    assert rec is None
    assert task.status == TaskStatus.pending
