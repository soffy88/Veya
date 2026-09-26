from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.flow_goal_run import GenesisGoalRunAdapter, phase3_task
from server.schemas import GenesisManifest, ThreeOElementRequest


def _manifest() -> GenesisManifest:
    return GenesisManifest(
        mission_id="mission-test",
        elements=[
            ThreeOElementRequest(layer="oskill", name="demo.py", specs="a demo element"),
        ],
    )


@pytest.mark.asyncio
async def test_genesis_phase3_projects_progress_into_goalrun_checkpoint(tmp_path, monkeypatch):
    import server.agents.genesis_agent as genesis_module
    import server.flow_goal_run as module

    calls: list[str] = []

    class FakeGenesis:
        def __init__(self, **_kwargs):
            pass

        def wake_up(self):
            calls.append("wake")

        async def handle_mission(self, mission):
            calls.append(mission)
            return {"status": "completed", "summary": "forged"}

        def sleep(self):
            calls.append("sleep")

    monkeypatch.setattr(genesis_module, "GenesisAgent", FakeGenesis)

    async def _llm_call(*args, **kwargs):
        return {"choices": [{"message": {"content": "print('assembled')"}}]}

    monkeypatch.setattr(module, "llm_call", _llm_call)
    monkeypatch.setattr(module, "emit", lambda *args, **kwargs: None)
    persisted: list[object] = []
    monkeypatch.setattr(
        module, "save_goal_run", lambda state, root: persisted.append((state, root))
    )

    state = SimpleNamespace(goal_id="goal-test", runtime_checkpoint=None)
    task = SimpleNamespace(instruction="unused")
    result = await GenesisGoalRunAdapter(
        _manifest(), project_root=str(tmp_path)
    ).execute_semantic_task(state, task)

    assert result.status == "completed"
    assert state.runtime_checkpoint["genesis_phase3"]["completed_elements"] == ["oskill:demo.py"]
    assert state.runtime_checkpoint["genesis_phase3"]["assembly_done"] is True
    assert (tmp_path / ".veya-project/goal-runs/goal-test/genesis/assembly.py").read_text() == (
        "print('assembled')"
    )
    assert calls[0] == "wake"
    assert calls[-1] == "sleep"
    assert persisted


@pytest.mark.asyncio
async def test_genesis_phase3_resume_skips_completed_element_and_assembly(tmp_path, monkeypatch):
    import server.flow_goal_run as module

    monkeypatch.setattr(module, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "save_goal_run", lambda *args, **kwargs: None)
    state = SimpleNamespace(
        goal_id="goal-resume",
        runtime_checkpoint={
            "genesis_phase3": {
                "completed_elements": ["oskill:demo.py"],
                "results": [{"layer": "oskill", "name": "demo.py", "status": "completed"}],
                "assembly_done": True,
                "assembly_artifact": "already-written.py",
            }
        },
    )
    result = await GenesisGoalRunAdapter(
        _manifest(), project_root=str(tmp_path)
    ).execute_semantic_task(state, SimpleNamespace(instruction="unused"))

    assert result.status == "completed"
    assert result.artifacts == ["already-written.py"]


def test_phase3_task_is_one_opaque_goalrun_leaf():
    task = phase3_task(_manifest())
    assert task["id"] == "genesis_phase3:mission-test"
    assert task["assignee"] == "builtin"
    assert '"mission_id": "mission-test"' in task["instruction"]


@pytest.mark.asyncio
async def test_failed_genesis_element_remains_retryable(tmp_path, monkeypatch):
    import server.agents.genesis_agent as genesis_module
    import server.flow_goal_run as module

    class FailingGenesis:
        def __init__(self, **_kwargs):
            pass

        def wake_up(self):
            pass

        async def handle_mission(self, _mission):
            raise RuntimeError("temporary Genesis failure")

        def sleep(self):
            pass

    monkeypatch.setattr(genesis_module, "GenesisAgent", FailingGenesis)
    monkeypatch.setattr(module, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "save_goal_run", lambda *args, **kwargs: None)

    state = SimpleNamespace(goal_id="goal-failure", runtime_checkpoint=None)
    result = await GenesisGoalRunAdapter(
        _manifest(), project_root=str(tmp_path)
    ).execute_semantic_task(state, SimpleNamespace(instruction="unused"))

    assert result.status == "blocked"
    assert state.runtime_checkpoint["genesis_phase3"]["completed_elements"] == []
