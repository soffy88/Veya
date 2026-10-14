"""B1 — Bot product runtime foundation acceptance.

Covers the Phase 1 acceptance list:
- BotSpec persistence + reload
- BotComputer persistence + bot isolation
- Main/Side Conversation
- BotMessage persistence + ordering
- BotGoal persistence + bot isolation (+ no execution state)
- BotSession persistence + conversation<->bot binding
- BotStatus aggregation
- cross-bot access denied
- restart/reload
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.bot_scope import CrossBotAccessDenied, require_same_bot
from runtime.computer.store import PersistentComputerStore
from server.bot_runtime.product import (
    BotComputerStatus,
    BotGoalStatus,
    BotProduct,
)
from server.goal_run.bot_identity import BotRegistry, BotSpec


def _product(tmp_path: Path) -> BotProduct:
    return BotProduct(tmp_path / "bots")


def _seed_computer(
    product: BotProduct,
    bot_id: str,
    *,
    computer_id: str,
    workspace_ref: str = "/ws",
    state: str = "created",
) -> None:
    """Seed a computer through the SOLE authority (never a second store)."""
    product.computers.ensure(
        bot_id=bot_id, owner_id="u1", workspace_ref=workspace_ref, computer_id=computer_id
    )
    if state != "created":
        product.computers.store.set_state(computer_id, state)


# ── BotSpec persistence + reload ────────────────────────────────────


def test_botspec_persists_and_reloads_with_product_fields(tmp_path: Path):
    registry = BotRegistry(tmp_path / "bot_registry.json")
    spec = BotSpec(
        bot_id="bot-alpha",
        owner_id="user-1",
        name="Alpha",
        avatar="a.png",
        description="video bot",
        personality="concise",
        runtime_id="agent_loop",
        computer_id="comp-alpha",
        default_workspace_id="ws-alpha",
        status="ready",
    )
    registry.register(spec)

    reloaded = BotRegistry(tmp_path / "bot_registry.json")
    got = reloaded.get("bot-alpha")
    assert got is not None
    assert got.name == "Alpha"
    assert got.personality == "concise"
    assert got.memory_namespace == "memory:bot-alpha"
    assert got.skill_registry_ref == "skills:bot-alpha"
    assert got.playbook_registry_ref == "playbooks:bot-alpha"
    assert got.computer_id == "comp-alpha"
    assert got.default_workspace_id == "ws-alpha"
    assert got.status == "ready"


# ── BotComputer persistence + bot isolation ─────────────────────────


def test_botcomputer_persists_and_reloads(tmp_path: Path):
    product = _product(tmp_path)
    _seed_computer(
        product,
        "bot-a",
        computer_id="comp-1",
        workspace_ref="/ws/a",
        state="running",
    )

    reloaded = _product(tmp_path)
    got = reloaded.computers.get("bot-a")
    assert got is not None
    assert got.computer_id == "comp-1"
    assert got.workspace_root == "/ws/a"
    assert got.status == BotComputerStatus.RUNNING

    # The record is durable in the authoritative SQLite substrate.
    authority = PersistentComputerStore(tmp_path / "bots" / "persistent_computers.sqlite3")
    assert authority.get_computer("comp-1", bot_id="bot-a") is not None


def test_botcomputer_is_isolated_per_bot(tmp_path: Path):
    product = _product(tmp_path)
    _seed_computer(product, "bot-a", computer_id="comp-a", workspace_ref="/ws/a")
    # Bot B cannot see bot A's computer.
    assert product.computers.get("bot-b") is None
    with pytest.raises(CrossBotAccessDenied):
        product.computers.require("bot-b", "comp-a")


# ── Main / Side conversation ────────────────────────────────────────


def test_main_and_side_conversations(tmp_path: Path):
    product = _product(tmp_path)
    main = product.conversations.create("bot-a", type="main", title="Main")
    side = product.conversations.create("bot-a", type="side", title="Research")
    assert main.type == "main"
    assert side.type == "side"
    assert product.conversations.main("bot-a").conversation_id == main.conversation_id
    assert len(product.conversations.list("bot-a")) == 2
    assert len(product.conversations.list("bot-a", type="side")) == 1
    # Only one main conversation per bot.
    with pytest.raises(ValueError):
        product.conversations.create("bot-a", type="main")


# ── BotMessage persistence + ordering ───────────────────────────────


def test_botmessage_persistence_and_ordering(tmp_path: Path):
    product = _product(tmp_path)
    conv = product.conversations.create("bot-a", type="main")
    product.messages.append("bot-a", conv.conversation_id, sender="user", content="hi")
    product.messages.append("bot-a", conv.conversation_id, sender="bot", content="hello")
    product.messages.append("bot-a", conv.conversation_id, sender="user", content="do it")

    reloaded = _product(tmp_path)
    messages = reloaded.messages.list("bot-a", conv.conversation_id)
    assert [m.sequence for m in messages] == [1, 2, 3]
    assert [m.content for m in messages] == ["hi", "hello", "do it"]

    # Cursor-style resume.
    tail = reloaded.messages.list("bot-a", conv.conversation_id, after_sequence=1)
    assert [m.content for m in tail] == ["hello", "do it"]


# ── BotGoal persistence + bot isolation (+ no execution state) ──────


def test_botgoal_persists_indexes_goalrun_and_has_no_execution_state(tmp_path: Path):
    product = _product(tmp_path)
    goal = product.goals.create(
        "bot-a", title="Ship feature", objective="ship it", constraints=["no force push"]
    )
    product.goals.update("bot-a", goal.goal_id, add_goal_run_id="goalrun-1")
    product.goals.update("bot-a", goal.goal_id, add_goal_run_id="goalrun-2")

    reloaded = _product(tmp_path)
    got = reloaded.goals.require("bot-a", goal.goal_id)
    assert got.status == BotGoalStatus.ACTIVE
    # GoalRun ids are an index only.
    assert got.goal_run_ids == ["goalrun-1", "goalrun-2"]

    # The goal dataclass must not carry execution state.
    field_names = set(got.to_dict())
    for forbidden in ("phase", "checkpoint", "tasks", "running_ids", "execution_state"):
        assert forbidden not in field_names


def test_botgoal_is_isolated_per_bot(tmp_path: Path):
    product = _product(tmp_path)
    goal = product.goals.create("bot-a", title="g")
    assert product.goals.get("bot-b", goal.goal_id) is None
    with pytest.raises(KeyError):
        product.goals.require("bot-b", goal.goal_id)


# ── BotSession persistence + conversation<->bot binding ─────────────


def test_botsession_binds_conversation_to_bot(tmp_path: Path):
    product = _product(tmp_path)
    conv = product.conversations.create("bot-a", type="main")
    session = product.sessions.create(
        "bot-a", conversation_id=conv.conversation_id, instance_id="inst-1"
    )
    reloaded = _product(tmp_path)
    got = reloaded.sessions.get("bot-a", session.session_id)
    assert got is not None
    assert got.bot_id == "bot-a"
    assert got.conversation_id == conv.conversation_id
    assert got.instance_id == "inst-1"
    assert [
        s.session_id for s in reloaded.sessions.list("bot-a", conversation_id=conv.conversation_id)
    ] == [session.session_id]


# ── BotStatus aggregation ───────────────────────────────────────────


def test_botstatus_aggregates_product_state(tmp_path: Path):
    product = _product(tmp_path)
    _seed_computer(product, "bot-a", computer_id="c", state="running")
    product.conversations.create("bot-a", type="main")
    conv2 = product.conversations.create("bot-a", type="side")
    product.goals.create("bot-a", title="g1")
    product.goals.create("bot-a", title="g2")
    product.sessions.create("bot-a", conversation_id=conv2.conversation_id)

    status = product.status("bot-a", lifecycle_status="running")
    assert status.bot_id == "bot-a"
    assert status.lifecycle_status == "running"
    assert status.computer_status == BotComputerStatus.RUNNING
    assert status.active_goal_count == 2
    assert status.active_session_count == 1
    assert status.conversation_count == 2


# ── cross-bot access denied ─────────────────────────────────────────


def test_cross_bot_access_denied_primitive():
    with pytest.raises(CrossBotAccessDenied):
        require_same_bot("bot-b", "bot-a", "goal:secret")


def test_cross_bot_records_are_invisible(tmp_path: Path):
    product = _product(tmp_path)
    _seed_computer(product, "bot-a", computer_id="comp-a")
    conv_a = product.conversations.create("bot-a", type="main")
    product.messages.append("bot-a", conv_a.conversation_id, sender="user", content="secret")

    # Bot B sees nothing of bot A.
    assert product.computers.get("bot-b") is None
    assert product.conversations.get("bot-b", conv_a.conversation_id) is None
    assert product.messages.list("bot-b", conv_a.conversation_id) == []
    assert product.goals.list("bot-b") == []


# ── restart / reload ────────────────────────────────────────────────


def test_full_product_survives_restart(tmp_path: Path):
    product = _product(tmp_path)
    _seed_computer(product, "bot-a", computer_id="c1", workspace_ref="/ws")
    conv = product.conversations.create("bot-a", type="main", title="Main")
    product.messages.append("bot-a", conv.conversation_id, sender="user", content="remember me")
    goal = product.goals.create("bot-a", title="persist", objective="survive restart")
    product.goals.update("bot-a", goal.goal_id, add_goal_run_id="gr-1")
    product.sessions.create("bot-a", conversation_id=conv.conversation_id, instance_id="inst-9")

    # Simulate process restart: brand-new facade over the same root.
    restarted = _product(tmp_path)
    assert restarted.computers.get("bot-a").computer_id == "c1"
    assert restarted.conversations.main("bot-a").title == "Main"
    assert restarted.messages.list("bot-a", conv.conversation_id)[0].content == "remember me"
    assert restarted.goals.require("bot-a", goal.goal_id).goal_run_ids == ["gr-1"]
    assert restarted.sessions.list("bot-a")[0].instance_id == "inst-9"
