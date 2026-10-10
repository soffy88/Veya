"""Lorca: ``message_bot`` as a model-visible tool.

Covers the one approved tool surface: that it is registered, that the source bot
comes from the running turn (and a model argument cannot name one), that the
self-call and unknown-bot refusals still apply, and that the hop limit holds when
bot messages nest inside other bot messages — the nested call is the tool running
inside the target bot's own turn, which is how a chain really forms.

The semantic authority is faked (``master_coordinator.execute_structured``);
GoalRun, DelegateRuntime and SpawnGuard are the real ones.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.bot_team.conversations import ConversationStore
from server.bot_team.messaging import MAX_HOP_DEPTH, BotMessageRefused
from server.bot_team.tool import (
    TOOL_NAME,
    current_hop_depth,
    message_bot,
    register_tools,
)
from server.events import EventStore
from server.goal_run.bot_identity import (
    BotRegistry,
    BotSpec,
    bind_current_bot,
    current_bot_id,
    reset_current_bot,
)
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.task_store import TaskStore

USER = {"user_id": "lorca-user", "username": "lorca"}


@pytest.fixture
def lorca_env(tmp_path, monkeypatch):
    """Every bot file lands in tmp_path; nothing touches the real ~/.veya."""

    bot_root = tmp_path / "bots-root"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    events = EventStore(tmp_path / "events.jsonl")
    tasks = TaskStore(tmp_path / "tasks.json", event_store=events)
    monkeypatch.setattr(product_routes, "task_store", tasks)

    class _History:
        async def save(self, session_id: str, messages: list[Any], *, user_id: str) -> None:
            return None

    monkeypatch.setattr(product_routes, "default_history_store", lambda: _History())
    return {"root": bot_root}


@pytest.fixture
def fake_master(monkeypatch):
    """Deterministic reply per bot: 'say:<bot id> <objective tail>'."""

    calls: list[Any] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        calls.append(request)
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    return calls


def _register(registry: BotRegistry, *bot_ids: str) -> BotRegistry:
    for bot_id in bot_ids:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=f"Does {bot_id} work.",
                instructions=f"You only do {bot_id} work.",
            )
        )
    return registry


@pytest.fixture
def registry(lorca_env) -> BotRegistry:
    return _register(BotRegistry(), "alpha", "beta", "gamma", "delta")


# ── registration ───────────────────────────────────────────────────────────


def test_message_bot_is_a_registered_tool():
    from server.tool_registry import master_tools

    assert master_tools.has(TOOL_NAME)

    schemas = [
        item for item in master_tools.get_all_schemas() if item["function"]["name"] == TOOL_NAME
    ]
    assert len(schemas) == 1, "exactly one tool surface was approved"
    function = schemas[0]["function"]

    description = function["description"].lower()
    assert "bot" in description and "reply" in description
    parameters = function["parameters"]
    assert set(parameters["properties"]) == {"target_bot_id", "text"}
    assert parameters["required"] == ["target_bot_id", "text"]
    # The source bot is not an argument: a model cannot name who is speaking.
    assert "source_bot_id" not in parameters["properties"]
    assert "depth" not in parameters["properties"]

    # It crosses the same governed boundary as comparable tools.
    policy = master_tools.side_effect_policy_for(TOOL_NAME)
    assert policy["side_effect"] == "process_exec"
    assert policy["effect_capability"] == "manual_only"


def test_register_tools_is_idempotent(lorca_env):
    from server.tool_registry import MasterToolRegistry

    fresh = MasterToolRegistry()
    assert register_tools(fresh) == 1
    assert register_tools(fresh) == 0
    assert fresh.list_tools() == [TOOL_NAME]


# ── the source bot comes from the running turn ────────────────────────────


@pytest.mark.asyncio
async def test_the_tool_speaks_as_the_bot_of_the_running_turn(lorca_env, fake_master, registry):
    del registry
    _register(BotRegistry(), "alpha", "beta")

    token = bind_current_bot("alpha")
    try:
        assert current_bot_id() == "alpha"
        payload = json.loads(await message_bot("beta", "summarise this"))
    finally:
        reset_current_bot(token)

    assert payload["source_bot_id"] == "alpha"
    assert payload["target_bot_id"] == "beta"
    assert payload["status"] == "complete"
    assert payload["reply"] == "say:beta"
    # The target ran one real turn, bound to the target bot.
    assert len(fake_master) == 1
    assert fake_master[0].constraints.metadata["bot_id"] == "beta"

    store = ConversationStore(lorca_env["root"])
    assert [item["text"] for item in store.list("alpha")] == ["say:beta"]
    assert [item["text"] for item in store.list("beta")] == ["summarise this", "say:beta"]


@pytest.mark.asyncio
async def test_the_tool_refuses_without_a_running_bot(lorca_env, fake_master):
    del fake_master
    _register(BotRegistry(), "alpha", "beta")

    assert current_bot_id() is None
    with pytest.raises(BotMessageRefused, match="no running bot"):
        await message_bot("beta", "hello")


@pytest.mark.asyncio
async def test_a_model_argument_cannot_name_the_source_bot(lorca_env, fake_master, registry):
    """Only the running turn may decide who is speaking."""

    del registry
    _register(BotRegistry(), "alpha", "beta")
    token = bind_current_bot("alpha")
    try:
        with pytest.raises(Exception, match="arguments failed JSON schema validation"):
            await master_execute(
                TOOL_NAME,
                {"target_bot_id": "beta", "text": "hi", "source_bot_id": "alpha"},
            )
    finally:
        reset_current_bot(token)
    assert fake_master == [], "a rejected call must not run a turn"


# ── the existing refusals still apply ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_tool_refuses_a_self_call(lorca_env, fake_master):
    del fake_master
    _register(BotRegistry(), "alpha")

    token = bind_current_bot("alpha")
    try:
        with pytest.raises(BotMessageRefused, match="cannot message itself"):
            await message_bot("alpha", "loop")
    finally:
        reset_current_bot(token)


@pytest.mark.asyncio
async def test_the_tool_refuses_an_unknown_target(lorca_env, fake_master):
    del fake_master
    _register(BotRegistry(), "alpha")

    token = bind_current_bot("alpha")
    try:
        with pytest.raises(BotMessageRefused, match="unknown bot: 'ghost'"):
            await message_bot("ghost", "hello")
    finally:
        reset_current_bot(token)


# ── the hop limit holds across nested tool calls ──────────────────────────


@pytest.mark.asyncio
async def test_the_hop_limit_holds_across_nested_tool_calls(lorca_env, monkeypatch, registry):
    """A chain of bot messages is refused at MAX_HOP_DEPTH, counting every hop.

    Each bot in the chain calls the tool from inside its own turn — exactly what
    happens when a bot asks a teammate, the teammate asks another, and so on. The
    hop depth travels with the execution context, so the innermost call is the
    one that hits the limit.
    """

    del registry
    _register(BotRegistry(), "alpha", "beta", "gamma", "delta")
    chain = {"alpha": "beta", "beta": "gamma", "gamma": "delta", "delta": "alpha"}
    turns: list[dict[str, Any]] = []
    refusals: list[str] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        turns.append({"bot": bot_id, "hop_depth": current_hop_depth()})
        target = chain[bot_id]
        try:
            # The model of this bot asks the next bot for work.
            await message_bot(target, f"question from {bot_id}")
        except BotMessageRefused as exc:
            refusals.append(str(exc))
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)

    token = bind_current_bot("alpha")
    try:
        payload = json.loads(await message_bot("beta", "start the chain"))
    finally:
        reset_current_bot(token)

    # Three hops are allowed (depth 0,1,2); the fourth call is refused, so no
    # turn of alpha ever runs from the deepest bot.
    assert [item["bot"] for item in turns] == ["beta", "gamma", "delta"]
    assert [item["hop_depth"] for item in turns] == [1, 2, 3]
    assert payload["source_bot_id"] == "alpha"
    assert payload["target_bot_id"] == "beta"
    assert payload["reply"] == "say:beta"
    assert len(refusals) == 1
    assert "hop limit" in refusals[0]
    assert f"max={MAX_HOP_DEPTH}" in refusals[0]


@pytest.mark.asyncio
async def test_two_messages_from_one_turn_do_not_accumulate_hop_depth(
    lorca_env, fake_master, monkeypatch, registry
):
    """Hop depth is per chain, not per turn.

    A bot that messages two teammates in one turn starts the chain fresh both
    times: the depth the target inherits is reset after every message, so the
    second teammate is not refused for the first message's hop.
    """

    del registry
    _register(BotRegistry(), "alpha", "beta", "gamma")
    turns: list[dict[str, Any]] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        turns.append({"bot": bot_id, "hop_depth": current_hop_depth()})
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)

    token = bind_current_bot("alpha")
    try:
        first = json.loads(await message_bot("beta", "first question"))
        second = json.loads(await message_bot("gamma", "second question"))
    finally:
        reset_current_bot(token)

    assert first["target_bot_id"] == "beta"
    assert second["target_bot_id"] == "gamma"
    assert [item["bot"] for item in turns] == ["beta", "gamma"]
    assert [item["hop_depth"] for item in turns] == [1, 1]


async def master_execute(name: str, arguments: dict[str, Any]) -> str:
    """Run one tool through the real registry entry (schema + guard + dispatch)."""

    from server.tool_registry import master_tools

    return await master_tools.execute(name, arguments)
