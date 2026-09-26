from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.automata_goal_run import GridSearchGoalRunAdapter, grid_search_task


@pytest.mark.asyncio
async def test_grid_search_projects_results_and_summary_into_goalrun(tmp_path, monkeypatch):
    import server.automata_goal_run as module

    class FakeQuant:
        async def execute_grid_search(self, strategy, asset, grid, progress_callback):
            assert (strategy, asset, grid) == ("strategy", "BTC", {"window": [5]})
            progress_callback(1, 1, {"sharpe": 1.2})
            return [{"params": {"window": 5}, "sharpe": 1.2}]

    class FakeOprim:
        def reduce_best(self, results):
            return results[0]

    class FakeScheduler:
        async def execute_callback(self, prompt):
            assert "BTC" in prompt
            return "summary"

    class FakeAutomata:
        _scheduler = FakeScheduler()

    notifications: list[tuple[object, ...]] = []
    monkeypatch.setattr(module, "save_goal_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("server.quant_coprocessor.quant_coprocessor", FakeQuant(), raising=False)
    monkeypatch.setattr("veya.platform.oprim", lambda: FakeOprim())
    monkeypatch.setattr("server.automata.get_automata", lambda: FakeAutomata())
    monkeypatch.setattr(
        "server.notification_center.global_notifier.push",
        lambda *args, **kwargs: notifications.append(args),
    )

    state = SimpleNamespace(goal_id="grid-goal", runtime_checkpoint={})
    task = SimpleNamespace(
        id="grid_search:test",
        instruction=grid_search_task("test", "BTC", "strategy", {"window": [5]}, "session")[
            "instruction"
        ],
    )
    result = await GridSearchGoalRunAdapter(project_root=str(tmp_path)).execute_semantic_task(
        state, task
    )

    assert result.status == "completed"
    assert result.summary == "summary"
    assert state.runtime_checkpoint["grid_search"]["best"]["sharpe"] == 1.2
    artifact = tmp_path / ".veya-project/goal-runs/grid-goal/grid_search.json"
    assert artifact.exists()
    assert any(item[0] == "SUCCESS" for item in notifications)


def test_grid_search_task_is_one_opaque_goalrun_leaf():
    task = grid_search_task("task", "BTC", "strategy", {"window": [5]}, "session")
    assert task["id"] == "grid_search:task"
    assert task["assignee"] == "builtin"
    assert '"asset_id": "BTC"' in task["instruction"]
