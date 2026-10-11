"""Tests for BotManager.create_bot() — L0-1 Creation proof.

Verifies that create_bot() produces BotSpec + AgentDefinition + AgentDeployment,
all three exist and are properly linked.
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


class TestBotCreation:
    """L0-1: create_bot() must produce BotSpec + AgentDefinition + AgentDeployment."""

    def test_create_bot_produces_all_three(
        self, manager: BotManager, bot_registry: BotRegistry, definition_store: AgentDefinitionStore
    ):
        """L0-1: create_bot() produces BotSpec, AgentDefinition, and AgentDeployment."""
        handle = manager.create_bot(
            bot_id="test-bot-1",
            owner_id="user-1",
            runtime_id="genesis",
            name="Test Bot",
            description="A test bot",
            instruction_refs=("instr-1",),
            skill_refs=("skill-1",),
        )

        # All three must exist
        assert handle.bot_id == "test-bot-1"
        assert handle.definition_id != ""
        assert handle.definition_version == 1
        assert handle.deployment_id != ""
        assert handle.deployment_revision == 1
        assert handle.runtime_id == "genesis"
        assert handle.status == "ready"

        # BotSpec exists in registry
        spec = bot_registry.get("test-bot-1")
        assert spec is not None
        assert spec.bot_id == "test-bot-1"
        assert spec.definition_id == handle.definition_id
        assert spec.definition_version == 1
        assert spec.deployment_id == handle.deployment_id
        assert spec.deployment_revision == 1
        assert spec.runtime_id == "genesis"
        assert spec.has_definition is True
        assert spec.has_deployment is True
        assert spec.has_runtime is True

        # AgentDefinition exists in store
        definition = definition_store.get_definition(handle.definition_id, 1)
        assert definition is not None
        assert definition.definition_id == handle.definition_id
        assert definition.version == 1
        assert definition.name == "Test Bot"
        assert definition.instruction_refs == ("instr-1",)
        assert definition.skill_refs == ("skill-1",)
        assert definition.metadata.get("bot_id") == "test-bot-1"
        assert definition.metadata.get("owner_id") == "user-1"

        # AgentDeployment exists in store
        deployment = definition_store.get_deployment(handle.deployment_id, 1)
        assert deployment is not None
        assert deployment.deployment_id == handle.deployment_id
        assert deployment.definition_id == handle.definition_id
        assert deployment.definition_version == 1
        assert deployment.runtime_id == "genesis"
        assert deployment.revision == 1
        assert deployment.status is AgentDeploymentStatus.ACTIVE

    def test_create_bot_definition_deployment_linkage(
        self, manager: BotManager, definition_store: AgentDefinitionStore
    ):
        """BotSpec, AgentDefinition, and AgentDeployment must be properly linked."""
        handle = manager.create_bot(
            bot_id="test-bot-2",
            owner_id="user-2",
            runtime_id="agent_loop",
            policy_refs=("policy-1",),
        )

        # Deployment pins the correct definition
        deployment = definition_store.get_deployment(handle.deployment_id, 1)
        assert deployment is not None
        assert deployment.definition_id == handle.definition_id
        assert deployment.definition_version == 1

        # Definition has the correct metadata
        definition = definition_store.get_definition(handle.definition_id, 1)
        assert definition is not None
        assert definition.metadata.get("bot_id") == "test-bot-2"

    def test_create_bot_idempotent(self, manager: BotManager):
        """Creating a bot with the same ID after deployment raises."""
        manager.create_bot(
            bot_id="test-bot-3",
            owner_id="user-3",
            runtime_id="genesis",
        )
        with pytest.raises(ValueError, match="already exists"):
            manager.create_bot(
                bot_id="test-bot-3",
                owner_id="user-3",
                runtime_id="genesis",
            )

    def test_create_bot_missing_bot_id(self, manager: BotManager):
        """Missing bot_id raises."""
        with pytest.raises(ValueError, match="bot_id is required"):
            manager.create_bot(
                bot_id="",
                owner_id="user-1",
                runtime_id="genesis",
            )

    def test_create_bot_missing_owner_id(self, manager: BotManager):
        """Missing owner_id raises."""
        with pytest.raises(ValueError, match="owner_id is required"):
            manager.create_bot(
                bot_id="test-bot-4",
                owner_id="",
                runtime_id="genesis",
            )

    def test_create_bot_missing_runtime_id(self, manager: BotManager):
        """Missing runtime_id raises."""
        with pytest.raises(ValueError, match="runtime_id is required"):
            manager.create_bot(
                bot_id="test-bot-5",
                owner_id="user-5",
                runtime_id="",
            )

    def test_create_bot_get_bot(self, manager: BotManager):
        """get_bot() returns the correct handle."""
        manager.create_bot(
            bot_id="test-bot-6",
            owner_id="user-6",
            runtime_id="genesis",
        )
        handle = manager.get_bot("test-bot-6")
        assert handle is not None
        assert handle.bot_id == "test-bot-6"
        assert handle.definition_id != ""
        assert handle.deployment_id != ""

    def test_create_bot_list_bots(self, manager: BotManager):
        """list_bots() returns all created bots."""
        manager.create_bot(bot_id="bot-a", owner_id="user-1", runtime_id="genesis")
        manager.create_bot(bot_id="bot-b", owner_id="user-1", runtime_id="agent_loop")
        manager.create_bot(bot_id="bot-c", owner_id="user-2", runtime_id="genesis")

        all_bots = manager.list_bots()
        assert len(all_bots) == 3

        user1_bots = manager.list_bots(owner_id="user-1")
        assert len(user1_bots) == 2
        bot_ids = {b.bot_id for b in user1_bots}
        assert "bot-a" in bot_ids
        assert "bot-b" in bot_ids
