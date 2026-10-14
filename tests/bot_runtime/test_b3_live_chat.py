"""B3 — live Bot chat over the real execution control plane.

Proves the end-to-end chain with nothing mocked below the semantic seam:

    bot.chat -> GoalRun -> ExecutionAdmission -> Local2 -> ExecutionControl
             -> response -> MemoryController -> ExecutionActivity -> ExecutionReceipt
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent_definition import AgentDefinitionStore
from server.bot_runtime.chat import BotChatService
from server.bot_runtime.manager import BotManager
from server.bot_runtime.product import BotProduct
from server.goal_run.bot_identity import BotRegistry
from server.goal_run.execution_activity import ActivityRecorder
from server.goal_run.execution_admission import reset_admissions, reset_scopes
from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run
from server.memory_controller import MemoryController, _MemoryStore
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


def _service(tmp_path: Path, mock_runtime_available) -> tuple[BotManager, BotChatService]:
    product = BotProduct(tmp_path / "bots")
    manager = BotManager(
        BotRegistry(storage_path=tmp_path / "bot_registry.json"),
        AgentDefinitionStore(tmp_path / "definitions"),
        product=product,
    )
    manager.create_bot(bot_id="bot-live", owner_id="u1", runtime_id="genesis", name="Live")
    memory = MemoryController(_MemoryStore(tmp_path / "memory.json"))

    async def semantic(message, context):
        # Deterministic semantic decision; the execution itself is real.
        return "", message

    service = BotChatService(
        manager, str(tmp_path), product=product, memory=memory, semantic_fn=semantic
    )
    return manager, service


@pytest.mark.asyncio
async def test_live_chat_full_chain(tmp_path: Path, mock_runtime_available):
    _manager, service = _service(tmp_path, mock_runtime_available)
    result = await service.chat("bot-live", "echo bot-live-ok")

    # Real Local2 execution produced the response.
    assert result.response.strip().endswith("bot-live-ok"), result.response
    assert result.status == GoalStatus.completed.value

    # GoalRun durable authority really advanced.
    state = load_goal_run(str(tmp_path), result.goal_run_id)
    assert state is not None
    assert state.status == GoalStatus.completed
    assert state.bot_id == "bot-live"

    # Admission + binding really happened, and the run was bound to them.
    admission_dir = tmp_path / ".veya" / "execution_admission"
    assert list(admission_dir.glob("admission_*.json"))
    assert list(admission_dir.glob("binding_*.json"))
    checkpoint = state.runtime_checkpoint or {}
    assert checkpoint.get("admission_id")
    assert checkpoint.get("binding_id")


@pytest.mark.asyncio
async def test_live_chat_persists_messages_and_session(tmp_path: Path, mock_runtime_available):
    _, service = _service(tmp_path, mock_runtime_available)
    result = await service.chat("bot-live", "echo persist")

    messages = service.product.messages.list("bot-live", result.conversation_id)
    assert [m.sender for m in messages] == ["user", "bot"]
    assert messages[0].content == "echo persist"
    assert messages[1].content.strip().endswith("persist")

    sessions = service.product.sessions.list("bot-live", conversation_id=result.conversation_id)
    assert len(sessions) == 1
    assert sessions[0].bot_id == "bot-live"


@pytest.mark.asyncio
async def test_live_chat_writes_memory_and_activity_and_receipt(
    tmp_path: Path, mock_runtime_available
):
    _, service = _service(tmp_path, mock_runtime_available)
    result = await service.chat("bot-live", "echo evidence")

    # Memory write.
    assert result.memory_id
    assert service._memory.get(result.memory_id) is not None

    # Activity stream (unified evidence).
    recorder = ActivityRecorder(str(tmp_path))
    activities, _cursor = recorder.stream(result.goal_run_id, cursor=0)
    types = [a.type for a in activities]
    assert "PROCESS_STARTED" in types
    assert "OUTPUT" in types
    assert "GOALRUN_COMPLETED" in types
    assert [a.sequence for a in activities] == sorted(a.sequence for a in activities)

    # Durable receipt.
    assert result.receipt
    for field_name in (
        "goal_run_id",
        "admission_id",
        "binding_id",
        "executor_id",
        "provider_run_id",
        "final_state",
    ):
        assert field_name in result.receipt


@pytest.mark.asyncio
async def test_live_chat_second_turn_reuses_main_conversation(
    tmp_path: Path, mock_runtime_available
):
    _, service = _service(tmp_path, mock_runtime_available)
    first = await service.chat("bot-live", "echo one")
    second = await service.chat("bot-live", "echo two")
    assert first.conversation_id == second.conversation_id
    messages = service.product.messages.list("bot-live", first.conversation_id)
    assert [m.sequence for m in messages] == [1, 2, 3, 4]
    # Two independent GoalRuns, same bot/conversation.
    assert first.goal_run_id != second.goal_run_id


@pytest.mark.asyncio
async def test_live_chat_goal_index_links_goalrun(tmp_path: Path, mock_runtime_available):
    _manager, service = _service(tmp_path, mock_runtime_available)
    product = service.product
    goal = product.goals.create("bot-live", title="long-term", objective="keep working")
    # A chat turn links its GoalRun into the goal index (ref only).
    result = await service.chat("bot-live", "echo link")
    product.goals.update("bot-live", goal.goal_id, add_goal_run_id=result.goal_run_id)
    reloaded = product.goals.require("bot-live", goal.goal_id)
    assert result.goal_run_id in reloaded.goal_run_ids
    # The goal record still carries no execution state.
    assert "phase" not in reloaded.to_dict()
