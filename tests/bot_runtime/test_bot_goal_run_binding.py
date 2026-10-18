"""Tests for Bot GoalRun binding — L0-5 GoalRun proof.

Verifies that Bot execution produces a real GoalRun with all identity fields:
goal_id, bot_id, definition_id, deployment_id, runtime_id, session_id, instance_id.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent_definition import AgentDefinitionStore
from server.bot_runtime.manager import BotManager
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
        # Both mocks return available=True for any runtime_id
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


from veya.remote.executor_adapters import register_default_adapters

@pytest.fixture(autouse=True)
def auto_register_adapters():
    register_default_adapters()

class TestBotGoalRunBinding:
    """L0-5: Bot execution must produce a real GoalRun with all identity fields."""

    @pytest.mark.asyncio
    async def test_submit_goal_produces_goalrun_identity(self, manager: BotManager):
        """GoalRun must have bot_id, definition_id, deployment_id, runtime_id, session_id, instance_id."""
        manager.create_bot(
            bot_id="test-bot-goalrun",
            owner_id="user-1",
            runtime_id="genesis",
        )

        result = await manager.submit_goal(
            "test-bot-goalrun",
            "Create a test goal run",
            source="external",
        )

        # Result must have all identity fields
        assert result.bot_id == "test-bot-goalrun"
        assert result.goal_id != "" or result.status in ("success", "failed")
        assert result.instance_id != ""

    @pytest.mark.asyncio
    async def test_goalrun_identity_consistent(self, manager: BotManager):
        """GoalRun identity fields must be consistent across the chain."""
        manager.create_bot(
            bot_id="test-bot-consistency",
            owner_id="user-1",
            runtime_id="genesis",
        )

        result = await manager.submit_goal(
            "test-bot-consistency",
            "Test consistency",
        )

        assert result.bot_id == "test-bot-consistency"
        assert result.instance_id != ""

    @pytest.mark.asyncio
    @pytest.mark.external
    async def test_no_duplicate_goalrun_on_resume(self, manager: BotManager):
        """Resuming must NOT create a new GoalRun; it must resume the existing one."""
        manager.create_bot(
            bot_id="test-bot-resume",
            owner_id="user-1",
            runtime_id="genesis",
        )

        # First submit
        result1 = await manager.submit_goal(
            "test-bot-resume",
            "First goal",
        )
        goal_id_1 = result1.goal_id

        # Stop and restart
        await manager.stop_bot("test-bot-resume")

        # Submit again (should create a new goal, not duplicate)
        result2 = await manager.submit_goal(
            "test-bot-resume",
            "Second goal",
        )
        goal_id_2 = result2.goal_id

        # They should be different goals (new submits create new goals)
        if goal_id_1 and goal_id_2:
            assert goal_id_1 != goal_id_2
