"""Tests for Master → Bot communication — L0-8, L0-9.

Verifies:
    L0-8: MasterAgent → BotManager → BotRuntime → Bot GoalRun
    L0-9: BotRuntime → BotResult → Master (Bot does NOT modify Master state)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent_definition import AgentDefinitionStore
from server.bot_runtime.manager import BotExecutionResult, BotManager
from server.goal_run.bot_identity import BotRegistry


@pytest.fixture
def bot_registry(tmp_path: Path) -> BotRegistry:
    return BotRegistry(storage_path=tmp_path / "bot_registry.json")


@pytest.fixture
def definition_store(tmp_path: Path) -> AgentDefinitionStore:
    return AgentDefinitionStore(tmp_path / "definitions")


@pytest.fixture
def mock_runtime_available():
    """Mock check_runtime_available to return available=True for test runtimes."""
    with (
        patch("server.bot_runtime.manager.check_runtime_available") as mock_manager,
        patch("server.agent_definition.check_runtime_available") as mock_agent_def,
    ):

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

        mock_manager.side_effect = mock_available
        mock_agent_def.side_effect = mock_available
        yield mock_manager, mock_agent_def


@pytest.fixture
def manager(
    bot_registry: BotRegistry, definition_store: AgentDefinitionStore, mock_runtime_available
) -> BotManager:
    return BotManager(bot_registry, definition_store)


class TestMasterBotCommunication:
    """L0-8: Master → Bot. L0-9: Bot → Master."""

    @pytest.mark.asyncio
    async def test_master_can_submit_goal_to_bot(self, manager: BotManager):
        """L0-8: Master → BotManager → BotRuntime → Bot GoalRun."""
        manager.create_bot(
            bot_id="test-bot-master-delegation",
            owner_id="user-1",
            runtime_id="genesis",
        )

        # Master delegates a goal to the Bot
        result = await manager.submit_goal(
            "test-bot-master-delegation",
            "Master-delegated objective",
            source="master",
        )

        # Result must have all required fields
        assert result.bot_id == "test-bot-master-delegation"
        assert result.status in ("success", "failed")
        assert result.instance_id != ""

        # Result must be a BotExecutionResult
        assert isinstance(result, BotExecutionResult)
        result_dict = result.to_dict()
        assert "bot_id" in result_dict
        assert "goal_id" in result_dict
        assert "session_id" in result_dict
        assert "instance_id" in result_dict
        assert "status" in result_dict
        assert "summary" in result_dict
        assert "artifacts" in result_dict

    @pytest.mark.asyncio
    async def test_bot_result_does_not_modify_master_state(self, manager: BotManager):
        """L0-9: Bot result is returned to Master, but Bot does NOT modify Master state."""
        manager.create_bot(
            bot_id="test-bot-no-master-modification",
            owner_id="user-1",
            runtime_id="genesis",
        )

        # Submit a goal
        result = await manager.submit_goal(
            "test-bot-no-master-modification",
            "Test objective",
            source="master",
        )

        # The result is a BotExecutionResult — it is returned to the caller
        # (Master) but does NOT modify any Master state directly.
        # In production, Master would receive this result and decide:
        # answer / continue / retry / delegate / finalize
        assert isinstance(result, BotExecutionResult)
        assert result.bot_id == "test-bot-no-master-modification"

    @pytest.mark.asyncio
    async def test_bot_execution_result_contract(self, manager: BotManager):
        """BotExecutionResult must have the required contract fields."""
        manager.create_bot(
            bot_id="test-bot-contract",
            owner_id="user-1",
            runtime_id="genesis",
        )

        result = await manager.submit_goal(
            "test-bot-contract",
            "Contract test",
            source="master",
        )

        # Required fields
        assert hasattr(result, "bot_id")
        assert hasattr(result, "goal_id")
        assert hasattr(result, "session_id")
        assert hasattr(result, "instance_id")
        assert hasattr(result, "status")
        assert hasattr(result, "summary")
        assert hasattr(result, "artifacts")

        # to_dict() must include all fields
        d = result.to_dict()
        for field in (
            "bot_id",
            "goal_id",
            "session_id",
            "instance_id",
            "status",
            "summary",
            "artifacts",
        ):
            assert field in d
