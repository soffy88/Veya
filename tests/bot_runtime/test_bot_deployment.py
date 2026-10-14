"""Tests for Bot deployment — L0-2 Deployment proof.

Verifies the chain: Bot → active Deployment → runtime_id → runtime admission.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from server.agent_definition import (
    AgentDefinitionStore,
    AgentDeploymentStatus,
)
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


class TestBotDeployment:
    """L0-2: Bot → active Deployment → runtime_id → runtime admission."""

    def test_deploy_bot_creates_new_revision(
        self, manager: BotManager, definition_store: AgentDefinitionStore
    ):
        """deploy_bot() creates a new deployment revision."""
        handle = manager.create_bot(
            bot_id="test-bot-deploy",
            owner_id="user-1",
            runtime_id="genesis",
        )

        new_handle = manager.deploy_bot(
            "test-bot-deploy",
            runtime_id="agent_loop",
            revision=2,
        )

        assert new_handle.deployment_id != handle.deployment_id
        assert new_handle.deployment_revision == 2
        assert new_handle.runtime_id == "agent_loop"

        # New deployment is ACTIVE
        deployment = definition_store.get_deployment(new_handle.deployment_id, 2)
        assert deployment is not None
        assert deployment.status is AgentDeploymentStatus.ACTIVE
        assert deployment.runtime_id == "agent_loop"

        # Old deployment still exists (history retained)
        old_deployment = definition_store.get_deployment(handle.deployment_id, 1)
        assert old_deployment is not None

    def test_deploy_bot_updates_botspec(self, manager: BotManager, bot_registry: BotRegistry):
        """deploy_bot() updates the BotSpec binding."""
        manager.create_bot(
            bot_id="test-bot-deploy2",
            owner_id="user-1",
            runtime_id="genesis",
        )

        new_handle = manager.deploy_bot(
            "test-bot-deploy2",
            runtime_id="agent_loop",
            revision=2,
        )

        spec = bot_registry.get("test-bot-deploy2")
        assert spec is not None
        assert spec.deployment_id == new_handle.deployment_id
        assert spec.deployment_revision == 2
        assert spec.runtime_id == "agent_loop"

    def test_deploy_bot_missing_bot(self, manager: BotManager):
        """deploy_bot() with unknown bot raises."""
        with pytest.raises(ValueError, match="unknown bot"):
            manager.deploy_bot("nonexistent", runtime_id="genesis")

    def test_deploy_bot_missing_definition(
        self, manager: BotManager, bot_registry: BotRegistry, definition_store: AgentDefinitionStore
    ):
        """deploy_bot() with bot missing definition raises."""
        # Create a bot without definition binding (raw BotSpec)
        from server.goal_run.bot_identity import BotSpec

        spec = BotSpec(bot_id="no-def-bot", owner_id="user-1")
        bot_registry.register(spec)

        with pytest.raises(ValueError, match="no definition binding"):
            manager.deploy_bot("no-def-bot", runtime_id="genesis")
