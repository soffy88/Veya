"""GoalRun consumes explicit objectives and task nodes; it is not an intent router."""

from __future__ import annotations

import pytest


def test_goal_run_objective_boundary_does_not_reclassify_text():
    from server.goal_run.runner import _explicit_understanding

    result = _explicit_understanding("请修复登录并运行测试", "auto")
    assert result.decision == "act"
    assert result.interpretation == "请修复登录并运行测试"
    assert result.reasons == ["objective supplied by MasterAgent"]


@pytest.mark.asyncio
async def test_goal_run_compiles_explicit_tasks_without_expansion(tmp_path):
    from server.goal_run.planner import g1_plan

    state, response = await g1_plan(
        interpretation="opaque objective",
        assumptions=[],
        goal_text="opaque objective",
        project_root=str(tmp_path),
        explicit_tasks=[
            {
                "id": "review",
                "title": "Review",
                "instruction": "Inspect the requested files",
                "acceptance": ["review report exists"],
                "parallel": True,
            }
        ],
    )
    assert list(state.tasks) == ["review"]
    assert state.tasks["review"].parallel is True
    assert response.goal_id == state.goal_id
    assert (tmp_path / ".veya-project" / "goal-runs" / state.goal_id / "taskgraph.json").exists()
