"""C3 Step 3 — Bot dispatch is the first production caller of the plane.

Asserts the migration is real and single-path: exactly one plane dispatch, one
substrate execution, one completion receipt, one completion activity, and no
legacy direct ``ExecutionControl`` dispatch left in the Bot chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.bot_scope import CrossBotAccessDenied
from server.agent_definition import AgentDefinitionStore
from server.bot_runtime.execution import execute_bot_run
from server.bot_runtime.manager import BotManager
from server.bot_runtime.product import BotProduct
from server.goal_run.bot_identity import BotRegistry
from server.goal_run.execution_abi import (
    CanonicalExecutionPlane,
    CanonicalGoalExecutionRequest,
    ExecutionBindingRef,
)
from server.goal_run.execution_activity import ActivityRecorder
from server.goal_run.execution_admission import reset_admissions, reset_scopes
from server.goal_run.execution_control import ExecutionControl
from server.goal_run.execution_evidence import load_binding_record, load_durable_receipts
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run
from veya.remote.executor_adapters import register_default_adapters


@pytest.fixture(autouse=True)
def _reset():
    register_default_adapters()
    reset_admissions()
    reset_scopes()
    yield
    reset_admissions()
    reset_scopes()


@pytest.fixture
def mock_runtime_available():
    from unittest.mock import patch

    def mock_available(runtime_id):
        return {
            "available": True,
            "reason": "",
            "kind": "provider",
            "runtime_id": runtime_id,
            "protocol_family": "llm",
            "capabilities": [],
            "lifecycle": {},
        }

    with (
        patch("server.bot_runtime.manager.check_runtime_available") as m1,
        patch("server.agent_definition.check_runtime_available") as m2,
    ):
        m1.side_effect = mock_available
        m2.side_effect = mock_available
        yield m1, m2


def _manager(tmp_path: Path, mock_runtime_available) -> BotManager:
    return BotManager(
        BotRegistry(storage_path=tmp_path / "bot_registry.json"),
        AgentDefinitionStore(tmp_path / "definitions"),
        product=BotProduct(tmp_path / "bots"),
    )


def _bot(manager: BotManager, bot_id: str, tmp_path: Path) -> Path:
    workspace = tmp_path / "workspaces" / bot_id
    workspace.mkdir(parents=True, exist_ok=True)
    manager.create_bot(
        bot_id=bot_id,
        owner_id="u1",
        runtime_id="agent_loop",
        workspace_root=str(workspace),
    )
    return workspace


# ── 1 + 2: exactly one plane dispatch and one substrate execution ───────


async def test_bot_submit_traverses_plane_and_substrate_exactly_once(tmp_path, monkeypatch):
    calls = {"plane": 0, "run_task": 0}
    orig_plane = CanonicalExecutionPlane.execute
    orig_run = ExecutionControl.run_task

    async def plane_spy(self, *a, **k):
        calls["plane"] += 1
        return await orig_plane(self, *a, **k)

    async def run_spy(self, *a, **k):
        calls["run_task"] += 1
        return await orig_run(self, *a, **k)

    monkeypatch.setattr(CanonicalExecutionPlane, "execute", plane_spy)
    monkeypatch.setattr(ExecutionControl, "run_task", run_spy)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    await execute_bot_run(
        project_root=str(workspace),
        bot_id="bot-a",
        objective="echo once",
        session_id="sess",
        admission_executor="codex",
    )
    assert calls == {"plane": 1, "run_task": 1}


# ── 3 + 8: correlation and no duplicate evidence ────────────────────────


async def test_evidence_correlates_and_is_not_duplicated(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = await execute_bot_run(
        project_root=str(workspace),
        bot_id="bot-a",
        objective="echo correlate",
        session_id="sess",
        admission_executor="codex",
    )
    state = load_goal_run(str(workspace), outcome.goal_run_id)
    assert state is not None
    assert state.bot_id == "bot-a"
    binding_id = str(state.runtime_checkpoint["binding_id"])
    binding = load_binding_record(str(workspace), binding_id)
    assert binding is not None

    assert outcome.execution_id == state.execution_id
    assert outcome.receipt["binding_id"] == binding_id
    assert outcome.receipt["goal_run_id"] == outcome.goal_run_id
    assert outcome.receipt["provider_run_id"] == outcome.execution_id

    receipts = load_durable_receipts(str(workspace), outcome.goal_run_id)
    completions = [
        r
        for r in receipts
        if r.get("provider_run_id") == outcome.execution_id and r.get("final_state") != "running"
    ]
    assert len(completions) == 1, "exactly one completion receipt per execution"

    activities, _ = ActivityRecorder(str(workspace)).stream(outcome.goal_run_id, cursor=0)
    completions_act = [a for a in activities if a.type == "GOALRUN_COMPLETED"]
    assert len(completions_act) == 1, "exactly one completion activity"


# ── 4: cross-bot access remains denied ──────────────────────────────────


async def test_cross_bot_access_is_denied(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = await execute_bot_run(
        project_root=str(workspace),
        bot_id="bot-a",
        objective="echo a",
        session_id="sess",
        admission_executor="codex",
    )
    state = load_goal_run(str(workspace), outcome.goal_run_id)
    binding = load_binding_record(str(workspace), str(state.runtime_checkpoint["binding_id"]))
    foreign = CanonicalGoalExecutionRequest(
        goal_run_id=state.goal_id,
        task_id=next(iter(state.tasks), ""),
        bot_id="bot-b",
        admitted_executor=str(binding["executor_id"]),
        substrate=str(binding["substrate_id"]),
        binding=ExecutionBindingRef.from_record(binding),
        objective="echo a",
    )
    with pytest.raises(CrossBotAccessDenied):
        await CanonicalExecutionPlane().execute(foreign, project_root=str(workspace))


# ── 5: both executor and substrate identities stay correct ──────────────


async def test_native_substrate_identity(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = await execute_bot_run(
        project_root=str(workspace),
        bot_id="bot-a",
        objective="echo n",
        session_id="sess",
        admission_executor="codex",
    )
    assert outcome.receipt["executor_id"] == "codex"
    assert outcome.receipt["substrate_id"] == "local2"
    assert outcome.receipt["substrate_kind"] == "native_substrate"


async def test_normal_executor_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "veya.remote.tool_adapter._worker_command",
        lambda *a, **k: (["echo", "codex-ok"], {}),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = await execute_bot_run(
        project_root=str(workspace),
        bot_id="bot-a",
        objective="do work",
        session_id="sess",
        executor="codex",
        admission_executor="codex",
    )
    assert outcome.receipt["executor_id"] == "codex"
    assert outcome.receipt["substrate_id"] == "codex"
    assert outcome.goal_status is GoalStatus.completed


# ── 6: adapter failure is not success ───────────────────────────────────


async def test_adapter_failure_is_not_success(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = await execute_bot_run(
        project_root=str(workspace),
        bot_id="bot-a",
        objective="false",
        session_id="sess",
        admission_executor="codex",
    )
    assert outcome.goal_status is GoalStatus.failed
    assert outcome.status == "failed"


# ── 7: checkpoint / finalization unchanged ──────────────────────────────


async def test_checkpoint_and_finalization_unchanged(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outcome = await execute_bot_run(
        project_root=str(workspace),
        bot_id="bot-a",
        objective="echo cp",
        session_id="sess",
        admission_executor="codex",
    )
    state = load_goal_run(str(workspace), outcome.goal_run_id)
    # The execution identity is persisted on the canonical GoalRun.
    assert state.execution_id == outcome.execution_id
    assert state.status is GoalStatus.completed
    assert state.finished_at is not None


# ── 9 + 10: ordinary GoalRun path untouched, no legacy dispatch ─────────


def test_plane_is_only_used_by_bot_dispatch():
    """The plane has exactly one production importer: the Bot dispatch."""
    root = Path("server")
    importers = {
        str(p)
        for p in root.rglob("*.py")
        if "CanonicalExecutionPlane" in p.read_text(encoding="utf-8")
    }
    assert importers == {
        "server/bot_runtime/execution.py",
        "server/goal_run/execution_abi.py",
    }, importers


def test_ordinary_goalrun_path_does_not_use_the_plane():
    for rel in (
        "server/goal_run/runner.py",
        "server/goal_run/planner.py",
        "server/goal_run/leaf.py",
    ):
        source = Path(rel).read_text(encoding="utf-8")
        assert "CanonicalExecutionPlane" not in source
        assert "execution_abi" not in source
        assert "CanonicalGoalExecutionRequest" not in source


def test_bot_dispatch_no_longer_calls_execution_control_directly():
    source = Path("server/bot_runtime/execution.py").read_text(encoding="utf-8")
    assert "ExecutionControl(" not in source
    assert "run_task(" not in source


async def test_bot_manager_end_to_end_uses_plane(tmp_path, mock_runtime_available):
    """A real Bot run (manager path) still produces correlated durable evidence."""
    manager = _manager(tmp_path, mock_runtime_available)
    workspace = _bot(manager, "bot-e2e", tmp_path)

    result = await manager.submit_goal("bot-e2e", "echo e2e")
    assert result.goal_id
    assert result.receipt
    assert result.receipt["executor_id"] == "codex"
    assert result.receipt["substrate_id"] == "local2"

    state = load_goal_run(str(workspace), result.goal_id)
    assert state is not None
    assert state.bot_id == "bot-e2e"
    assert state.execution_id == result.execution_id
