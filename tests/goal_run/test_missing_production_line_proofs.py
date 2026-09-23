from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from server.automata_goal_run import GridSearchGoalRunAdapter, grid_search_task
from server.flow_goal_run import GenesisGoalRunAdapter, phase3_task
from server.goal_run.leaf import LeafResult
from server.goal_run.models import GoalStatus
from server.goal_run.runner import project_run_goal
from server.goal_run.verify import VerifyResult
from server.schemas import GenesisManifest, ThreeOElementRequest


@pytest.mark.asyncio
async def test_flow_phase3_reaches_terminal_goalrun(tmp_path, monkeypatch):
    import server.agents.genesis_agent as genesis_module
    import server.flow_goal_run as flow_module

    class FakeGenesis:
        def __init__(self, **_kwargs):
            pass

        def wake_up(self):
            return None

        async def handle_mission(self, _mission):
            return {"status": "completed", "summary": "forged"}

        def sleep(self):
            return None

    async def fake_llm(*_args, **_kwargs):
        return {"choices": [{"message": {"content": "print('assembled')"}}]}

    monkeypatch.setattr(genesis_module, "GenesisAgent", FakeGenesis)
    monkeypatch.setattr(flow_module, "llm_call", fake_llm)
    monkeypatch.setattr(flow_module, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("VEYA_GOAL_RUN_CODE_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    manifest = GenesisManifest(
        mission_id="qualification-flow",
        elements=[ThreeOElementRequest(layer="oskill", name="demo.py", specs="demo")],
    )
    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="flow phase3 qualification",
        tasks=[phase3_task(manifest)],
        mode="act_eager",
        integration_adapter=GenesisGoalRunAdapter(manifest, project_root=str(tmp_path)),
    )

    assert result.status == GoalStatus.completed
    state_path = tmp_path / ".veya-project" / "goal-runs" / result.goal_id / "taskgraph.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["tasks"][0]["status"] == "completed"
    assert data["tasks"][0]["id"].startswith("genesis_phase3:")


@pytest.mark.asyncio
async def test_automata_grid_reaches_terminal_goalrun(tmp_path, monkeypatch):
    class FakeQuant:
        async def execute_grid_search(self, strategy, asset, grid, progress_callback):
            progress_callback(1, 1, {"sharpe": 1.0})
            return [{"params": {"window": 5}, "sharpe": 1.0}]

    class FakeOprim:
        def reduce_best(self, results):
            return results[0]

    class FakeScheduler:
        async def execute_callback(self, _prompt):
            return "grid summary"

    monkeypatch.setattr("server.quant_coprocessor.quant_coprocessor", FakeQuant())
    monkeypatch.setattr("veya.platform.oprim", lambda: FakeOprim())
    monkeypatch.setattr(
        "server.automata.get_automata", lambda: SimpleNamespace(_scheduler=FakeScheduler())
    )
    monkeypatch.setattr(
        "server.notification_center.global_notifier.push", lambda *_args, **_kwargs: None
    )
    monkeypatch.setenv("VEYA_GOAL_RUN_CODE_REVIEW_ENABLED", "0")
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="automata grid qualification",
        tasks=[grid_search_task("qualification", "BTC", "strategy", {"window": [5]}, "s")],
        mode="act_eager",
        integration_adapter=GridSearchGoalRunAdapter(project_root=str(tmp_path)),
    )

    assert result.status == GoalStatus.completed
    state_path = tmp_path / ".veya-project" / "goal-runs" / result.goal_id / "taskgraph.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["tasks"][0]["status"] == "completed"
    assert data["tasks"][0]["id"].startswith("grid_search:")


@pytest.mark.asyncio
async def test_advisory_review_cannot_block_goalrun_finalization(tmp_path, monkeypatch):
    async def fake_leaf(*_args, **_kwargs):
        return LeafResult(status="completed", summary="provider result")

    async def fake_verify(*_args, **_kwargs):
        return VerifyResult(passed=True, summary="accepted")

    async def advisory_finding(*_args, **_kwargs):
        return {"standards": {"worst": "advisory finding"}, "spec": {"worst": "advisory"}}

    monkeypatch.setattr("server.goal_run.runner.execute_leaf_with_memory", fake_leaf)
    monkeypatch.setattr("server.goal_run.runner.verify_task", fake_verify)
    monkeypatch.setattr("server.goal_run.runner._run_dual_axis_review", advisory_finding)
    monkeypatch.setenv("VEYA_GOAL_RUN_PLAN_REVIEW_ENABLED", "0")

    result = await project_run_goal(
        project_root=str(tmp_path),
        goal="advisory finalization proof",
        tasks=[{"id": "advisory", "instruction": "work", "acceptance": ["ok"]}],
        mode="act_eager",
    )

    assert result.status == GoalStatus.completed
    assert result.phase == "finalized"
