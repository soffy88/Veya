"""B2 — Bot computer bound to the SOLE computer authority.

Authority boundary under test (frozen)::

    PersistentComputerStore = sole computer authority
    BotComputer              = Bot-facing projection (DTO only)

Acceptance chain: Bot → AgentInstance → PersistentComputer (real SQLite) →
BotRuntime READY, with the computer acting as a real execution input
(``workspace_ref`` reaches the engine) and no second computer store anywhere.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.bot_scope import CrossBotAccessDenied
from runtime.computer.store import PersistentComputerStore
from server.agent_definition import AgentDefinitionStore
from server.bot_runtime.manager import BotManager
from server.bot_runtime.product import BotComputerProjection, BotComputerStatus, BotProduct
from server.goal_run.bot_identity import BotRegistry


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


def _bots_root(tmp_path: Path) -> Path:
    return tmp_path / "bots"


def _manager(tmp_path: Path, mock_runtime_available) -> BotManager:
    return BotManager(
        BotRegistry(storage_path=tmp_path / "bot_registry.json"),
        AgentDefinitionStore(tmp_path / "definitions"),
        product=BotProduct(_bots_root(tmp_path)),
    )


def _authority(tmp_path: Path) -> PersistentComputerStore:
    return PersistentComputerStore(_bots_root(tmp_path) / "persistent_computers.sqlite3")


# ── B2-1/B2-2: no duplicate store, BotComputer is a projection ────────────


def test_duplicate_computer_store_is_gone():
    """The JSON-per-bot computer store must not exist any more."""
    import server.bot_runtime.product as product

    assert not hasattr(product, "BotComputerStore")
    assert hasattr(product, "BotComputerProjection")


def test_bot_computer_is_projection_not_store(tmp_path: Path):
    """BotComputer carries no persistence: it can only be built from authority."""
    from server.bot_runtime.product import BotComputer

    assert not hasattr(BotComputer, "put")
    assert not hasattr(BotComputer, "from_dict")


def test_projection_reads_and_writes_through_authority(tmp_path: Path):
    root = _bots_root(tmp_path)
    projection = BotComputerProjection(PersistentComputerStore(root / "c.sqlite3"))

    projection.ensure(
        bot_id="bot-x",
        owner_id="u1",
        workspace_ref=str(root / "bot-x" / "workspace"),
        computer_id="comp-x",
    )
    # The projection did not persist anything itself — the authority did.
    assert (root / "c.sqlite3").exists()
    view = projection.get("bot-x")
    assert view is not None
    assert view.computer_id == "comp-x"
    assert view.workspace_root.endswith("bot-x/workspace")


def test_projection_refuses_cross_bot_read(tmp_path: Path):
    projection = BotComputerProjection(PersistentComputerStore(_bots_root(tmp_path) / "c.sqlite3"))
    projection.ensure(bot_id="bot-x", owner_id="u1", workspace_ref="/ws/x", computer_id="comp-x")
    with pytest.raises(CrossBotAccessDenied):
        projection.get_persistent("comp-x", bot_id="bot-y")


# ── B2-2: BotSpec.computer_id points at the authority ────────────────────


def test_create_bot_binds_spec_to_authority_record(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-alpha", owner_id="u1", runtime_id="genesis", name="Alpha")

    spec = manager._bot_registry.get("bot-alpha")
    assert spec is not None and spec.computer_id

    # The record really exists in the authoritative SQLite store.
    authority = _authority(tmp_path)
    stored = authority.get_computer(spec.computer_id, bot_id="bot-alpha")
    assert stored is not None
    assert stored.bot_id == "bot-alpha"
    assert stored.owner_id == "u1"
    assert stored.workspace_ref


def test_computer_record_is_really_in_sqlite(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-sql", owner_id="u1", runtime_id="genesis")

    db = _bots_root(tmp_path) / "persistent_computers.sqlite3"
    assert db.exists(), "computer authority must be a real SQLite database"

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT computer_id, bot_id, workspace_ref FROM persistent_computers"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "bot-sql"


def test_computer_survives_manager_restart(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-beta", owner_id="u1", runtime_id="genesis")
    original = manager.get_computer("bot-beta")
    assert original is not None

    # A fresh manager over the same durable substrate == process restart.
    restarted = _manager(tmp_path, mock_runtime_available)
    restored = restarted.ensure_computer("bot-beta")
    assert restored is not None
    assert restored.computer_id == original.computer_id
    assert restored.workspace_root == original.workspace_root


def test_computer_is_per_bot(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-a", owner_id="u1", runtime_id="genesis")
    manager.create_bot(bot_id="bot-b", owner_id="u1", runtime_id="genesis")

    a = manager.get_computer("bot-a")
    b = manager.get_computer("bot-b")
    assert a is not None and b is not None
    assert a.computer_id != b.computer_id
    assert a.workspace_root != b.workspace_root


# ── B2-3/B2-5/B2-6: BotRuntime binds the computer and reaches READY ──────


@pytest.mark.asyncio
async def test_start_bot_reaches_ready_with_bound_computer(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-ready", owner_id="u1", runtime_id="agent_loop")

    instance = await manager.start_bot("bot-ready")
    assert instance.status.value == "ready"
    assert instance.bot_id == "bot-ready"
    assert instance.instance_id

    runtime = manager.get_runtime("bot-ready")
    assert runtime is not None
    computer = runtime.computer
    assert computer is not None, "BotRuntime must bind the bot's computer"

    spec = manager._bot_registry.get("bot-ready")
    assert spec is not None
    assert computer.computer_id == spec.computer_id, "runtime binds BotSpec's computer"
    assert computer.bot_id == "bot-ready"
    assert computer.status == BotComputerStatus.RUNNING

    # The authoritative record reflects the running lifecycle.
    stored = _authority(tmp_path).get_computer(computer.computer_id, bot_id="bot-ready")
    assert stored is not None
    assert stored.lifecycle_state == "running"


@pytest.mark.asyncio
async def test_computer_workspace_reaches_the_engine(tmp_path: Path, mock_runtime_available):
    """The computer workspace is a real engine input, not decoration."""
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-ws", owner_id="u1", runtime_id="agent_loop")
    await manager.start_bot("bot-ws")

    runtime = manager.get_runtime("bot-ws")
    assert runtime is not None and runtime._engine is not None
    config = runtime._engine._config
    assert config is not None
    assert config.computer_id == runtime.computer.computer_id
    assert config.workspace_root == runtime.computer.workspace_root
    assert config.workspace_root, "engine must receive a non-empty workspace root"


@pytest.mark.asyncio
async def test_start_refuses_when_computer_is_terminated(tmp_path: Path, mock_runtime_available):
    """Fail-closed: a terminated computer must not admit a runtime."""
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-dead", owner_id="u1", runtime_id="agent_loop")

    computer = manager.get_computer("bot-dead")
    assert computer is not None
    _authority(tmp_path).set_state(computer.computer_id, "terminated")

    from server.bot_runtime.runtime import BotRuntimeError

    with pytest.raises(BotRuntimeError, match="terminated"):
        await manager.start_bot("bot-dead")


@pytest.mark.asyncio
async def test_agentloop_adapter_uses_real_agent_loop_abi(tmp_path: Path, mock_runtime_available):
    """B2-4: the adapter must build the real AgentLoop, not a missing helper."""
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-abi", owner_id="u1", runtime_id="agent_loop")
    await manager.start_bot("bot-abi")

    runtime = manager.get_runtime("bot-abi")
    assert runtime is not None
    engine = runtime._engine
    assert engine is not None

    from veya.omodul.agent_loop import AgentLoop

    assert isinstance(engine._loop, AgentLoop), "adapter must hold a real AgentLoop"


# ── takeover / release map to the authoritative lifecycle ─────────────────


def test_takeover_and_release_computer(tmp_path: Path, mock_runtime_available):
    manager = _manager(tmp_path, mock_runtime_available)
    manager.create_bot(bot_id="bot-gamma", owner_id="u1", runtime_id="genesis")

    computer = manager.get_computer("bot-gamma")
    assert computer is not None

    taken = manager.takeover_computer("bot-gamma")
    assert taken is not None
    assert _authority(tmp_path).get_computer(computer.computer_id).lifecycle_state == "stopped"

    released = manager.release_computer("bot-gamma")
    assert released is not None
    assert _authority(tmp_path).get_computer(computer.computer_id).lifecycle_state == "running"
