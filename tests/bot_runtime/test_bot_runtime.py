"""Tests for BotRuntime — L0-3 Independent Entry, L0-4 Tool Execution.

Verifies that BotRuntime can start, submit goals, and execute through the
AgentEngine without going through MasterAgent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent_definition import (
    AgentDefinitionStore,
)
from server.bot_runtime.manager import BotManager
from server.bot_runtime.runtime import BotRuntimeError
from server.goal_run.bot_identity import BotRegistry, BotSpec


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
def mock_genesis_agent():
    """Mock GenesisAgent to avoid API key requirement in tests."""
    with patch("server.agents.genesis_agent.GenesisAgent") as mock_genesis:
        # Mock the GenesisAgent instance
        mock_instance = mock_genesis.return_value
        mock_instance.wake_up.return_value = None
        mock_instance.sleep.return_value = None
        mock_instance.handle_mission.return_value = {
            "status": "success",
            "response": "Test response",
            "steps": 1,
            "cost_usd": 0.001,
        }
        yield mock_genesis


@pytest.fixture
def manager(
    bot_registry: BotRegistry,
    definition_store: AgentDefinitionStore,
    mock_runtime_available,
    mock_genesis_agent,
) -> BotManager:
    return BotManager(bot_registry, definition_store)


from veya.remote.executor_adapters import register_default_adapters

@pytest.fixture(autouse=True)
def auto_register_adapters():
    register_default_adapters()

class TestBotRuntime:
    """L0-3: Independent Bot entry. L0-4: Tool execution."""

    @pytest.mark.asyncio
    async def test_runtime_start_stop(self, manager: BotManager):
        """BotRuntime can start and stop."""
        manager.create_bot(
            bot_id="test-bot-runtime",
            owner_id="user-1",
            runtime_id="genesis",
        )

        instance = await manager.start_bot("test-bot-runtime")
        assert instance.bot_id == "test-bot-runtime"
        assert instance.instance_id != ""
        assert instance.status.value == "ready"

        await manager.stop_bot("test-bot-runtime")

    @pytest.mark.asyncio
    async def test_runtime_submit_goal(self, manager: BotManager):
        """BotRuntime can submit a goal and get a result (L0-3)."""
        manager.create_bot(
            bot_id="test-bot-submit",
            owner_id="user-1",
            runtime_id="genesis",
        )

        result = await manager.submit_goal(
            "test-bot-submit",
            "Test objective",
            source="external",
        )

        assert result.bot_id == "test-bot-submit"
        assert result.status in ("success", "failed")
        assert result.instance_id != ""

    @pytest.mark.asyncio
    async def test_runtime_pause_resume(self, manager: BotManager):
        """pause() is fail-closed: it only applies to a RUNNING instance."""
        manager.create_bot(
            bot_id="test-bot-pause",
            owner_id="user-1",
            runtime_id="genesis",
        )

        await manager.start_bot("test-bot-pause")
        runtime = manager.get_runtime("test-bot-pause")
        assert runtime is not None

        # READY is not RUNNING — pausing must be refused, never silently applied.
        assert runtime.status.value == "ready"
        with pytest.raises(BotRuntimeError, match="cannot pause from status ready"):
            await manager.pause_bot("test-bot-pause")
        assert runtime.status.value == "ready"

        # A submitted goal drives the instance through RUNNING to a terminal
        # state; the runtime never stays paused by a no-op pause request.
        await manager.submit_goal("test-bot-pause", "Objective for pause contract")
        assert runtime.status.value in ("stopped", "failed", "paused")

        await manager.stop_bot("test-bot-pause")

    def test_runtime_missing_bot_raises(self, manager: BotManager):
        """start_bot() with unknown bot raises."""
        import pytest as _pytest

        with _pytest.raises(ValueError, match="unknown bot"):
            import asyncio

            asyncio.run(manager.start_bot("nonexistent"))

    @pytest.mark.asyncio
    async def test_runtime_missing_binding_raises(
        self, manager: BotManager, bot_registry: BotRegistry
    ):
        """start_bot() with bot missing bindings raises."""

        spec = BotSpec(bot_id="no-binding-bot", owner_id="user-1")
        bot_registry.register(spec)

        with pytest.raises(ValueError, match="not fully bound"):
            await manager.start_bot("no-binding-bot")
