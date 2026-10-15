"""Lorca P2: bots message each other + group chats.

Covers the routing half of the feature: ``message_bot`` hop limit and
self-target refusal, the target running a normal turn through the existing
delegation runtime, group turn order, the PASS sentinel staying hidden, early
stop when everybody passes, and the group size limit.

The semantic authority is faked (``master_coordinator.execute_structured``);
GoalRun, DelegateRuntime and SpawnGuard are the real ones.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.conversations import PASS_SENTINEL, ConversationStore
from server.bot_team.groups import MAX_GROUP_MEMBERS, GroupStore
from server.bot_team.messaging import MAX_HOP_DEPTH, BotMessageRefused, BotMessenger
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.routes.bot_team import router as bots_router
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


@pytest.fixture
def client():
    app = FastAPI()
    # W11-AUTH: bot routes now require a signed-in user (D2).
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


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
    return _register(BotRegistry(), "alpha", "beta", "gamma")


# ── P2.1 message_bot ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_message_bot_refuses_self_target(lorca_env, fake_master):
    del fake_master
    registry = _register(BotRegistry(), "alpha")
    messenger = BotMessenger(registry)

    with pytest.raises(BotMessageRefused, match="cannot message itself"):
        await messenger.message_bot("alpha", "alpha", "hello", user=USER)


@pytest.mark.asyncio
async def test_message_bot_refuses_unknown_bots(lorca_env, fake_master):
    del fake_master
    registry = _register(BotRegistry(), "alpha")
    messenger = BotMessenger(registry)

    with pytest.raises(BotMessageRefused, match="unknown bot: 'ghost'"):
        await messenger.message_bot("alpha", "ghost", "hello", user=USER)
    with pytest.raises(BotMessageRefused, match="unknown bot: 'ghost'"):
        await messenger.message_bot("ghost", "alpha", "hello", user=USER)


@pytest.mark.asyncio
async def test_message_bot_hop_limit(lorca_env, fake_master):
    """A chain of bot messages may be at most MAX_HOP_DEPTH deep."""

    registry = _register(BotRegistry(), "alpha", "beta")
    messenger = BotMessenger(registry)

    # One hop below the limit still runs (the guard refuses depth >= max, so
    # the boundary is exact, not off by one).
    allowed = await messenger.message_bot(
        "alpha", "beta", "hello", user=USER, depth=MAX_HOP_DEPTH - 1
    )
    assert allowed.status == "complete"
    assert len(fake_master) == 1

    with pytest.raises(BotMessageRefused, match="hop limit"):
        await messenger.message_bot("alpha", "beta", "hello", user=USER, depth=MAX_HOP_DEPTH)


@pytest.mark.asyncio
async def test_message_bot_runs_the_target_turn_and_records_both_sides(lorca_env, fake_master):
    registry = _register(BotRegistry(), "alpha", "beta")
    messenger = BotMessenger(registry, conversations=ConversationStore(lorca_env["root"]))

    outcome = await messenger.message_bot("alpha", "beta", "please summarise this", user=USER)

    assert outcome.source_bot_id == "alpha"
    assert outcome.target_bot_id == "beta"
    assert outcome.status == "complete"
    assert outcome.reply == "say:beta"
    # The target ran one real product turn, bound to the target bot.
    assert len(fake_master) == 1
    assert fake_master[0].constraints.metadata["bot_id"] == "beta"

    store = ConversationStore(lorca_env["root"])
    beta_messages = store.list("beta")
    assert [item["role"] for item in beta_messages] == ["user", "bot"]
    assert beta_messages[1]["text"] == "say:beta"
    alpha_messages = store.list("alpha")
    assert [item["role"] for item in alpha_messages] == ["bot"]
    assert alpha_messages[0]["text"] == "say:beta"


@pytest.mark.asyncio
async def test_message_bot_over_http_refuses_and_routes(client, lorca_env, fake_master):
    client.get("/api/v1/bots")
    _register(BotRegistry(), "alpha", "beta")

    self_target = client.post(
        "/api/v1/bots/alpha/message_bot",
        json={"target_bot_id": "alpha", "text": "loop"},
    )
    assert self_target.status_code == 422
    assert "cannot message itself" in self_target.json()["detail"]

    unknown = client.post(
        "/api/v1/bots/alpha/message_bot",
        json={"target_bot_id": "ghost", "text": "hello"},
    )
    assert unknown.status_code == 422

    hop = client.post(
        "/api/v1/bots/alpha/message_bot",
        json={"target_bot_id": "beta", "text": "hello", "depth": MAX_HOP_DEPTH},
    )
    assert hop.status_code == 422
    assert "hop limit" in hop.json()["detail"]

    accepted = client.post(
        "/api/v1/bots/alpha/message_bot",
        json={"target_bot_id": "beta", "text": "please summarise this"},
    )
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["status"] == "complete"
    assert body["reply"] == "say:beta"
    # The target's goal run id is the delegation ref the runtime recorded.
    assert body["goal_run_id"].startswith("bot-turn:beta:")


# ── P2.2 group chats ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_turn_order_and_transcript(lorca_env, fake_master):
    registry = _register(BotRegistry(), "alpha", "beta")
    store = GroupStore(lorca_env["root"])
    group = store.create(name="Triple", bot_ids=["alpha", "beta"])

    result = await _run_group(group=group, registry=registry, store=store, text="plan the brief")

    assert [item["bot_id"] for item in result["replies"]] == ["alpha", "beta"]
    assert [item["reply"] for item in result["replies"]] == ["say:alpha", "say:beta"]
    assert [item["round"] for item in result["replies"]] == [1, 1]
    assert result["passed"] == []
    assert len(fake_master) == 2

    transcript = store.messages(group.id)
    assert [item["role"] for item in transcript] == ["user", "bot", "bot"]
    assert transcript[0]["text"] == "plan the brief"
    assert transcript[1]["bot_id"] == "alpha"
    assert transcript[2]["bot_id"] == "beta"
    # The second member received the first member's reply as context.
    assert "say:alpha" in fake_master[1].objective

    # Every member also recorded the turn in its own conversation.
    conversations = ConversationStore(lorca_env["root"])
    assert [item["text"] for item in conversations.list("beta")] == [
        "plan the brief",
        "say:beta",
    ]


@pytest.mark.asyncio
async def test_group_pass_reply_is_hidden_and_stops_the_round_early(lorca_env, monkeypatch):
    """A member with nothing to add passes; all-pass stops the round early."""

    registry = _register(BotRegistry(), "alpha", "beta", "gamma")
    store = GroupStore(lorca_env["root"])
    # Three rounds per message, but every member passes on round one.
    group = store.create(name="Quiet", bot_ids=["alpha", "beta"], rounds_per_message=3)

    calls: list[Any] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        calls.append(request)
        return GoalRunResponse(
            goal_id="goal-quiet",
            status=GoalStatus.completed,
            phase="finalized",
            summary=PASS_SENTINEL,
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)

    result = await _run_group(group=group, registry=registry, store=store, text="thoughts?")

    # One turn per member, then stop: no second or third round.
    assert result["rounds"] == 1
    assert result["passed"] == ["alpha", "beta"]
    assert result["replies"] == []
    assert len(calls) == 2

    visible = store.messages(group.id)
    assert [item["role"] for item in visible] == ["user"]
    hidden = store.messages(group.id, include_hidden=True)
    assert len(hidden) == 3
    assert all(item["hidden"] for item in hidden[1:])
    assert all(item["text"] == PASS_SENTINEL for item in hidden[1:])
    # The bot conversations keep the same rule: the question stays, the PASS
    # reply does not.
    conversations = ConversationStore(lorca_env["root"])
    assert [item["text"] for item in conversations.list("alpha")] == ["thoughts?"]
    hidden_alpha = conversations.list("alpha", include_hidden=True)
    assert len(hidden_alpha) == 2
    assert hidden_alpha[1]["text"] == PASS_SENTINEL
    assert hidden_alpha[1]["hidden"] is True


@pytest.mark.asyncio
async def test_group_stops_when_every_member_has_passed(lorca_env, monkeypatch):
    """A round continues while at least one member still has something to add."""

    registry = _register(BotRegistry(), "alpha", "beta", "gamma")
    store = GroupStore(lorca_env["root"])
    group = store.create(name="Mixed", bot_ids=["alpha", "beta", "gamma"], rounds_per_message=2)

    seen: list[str] = []

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        seen.append(bot_id)
        summary = PASS_SENTINEL if bot_id == "gamma" else f"say:{bot_id}"
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=summary,
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)

    result = await _run_group(group=group, registry=registry, store=store, text="ideas?")

    # Round 1: alpha, beta, gamma(pass). Not all passed, so round 2 runs and
    # gamma passes again → 5 turns, no third round.
    assert result["rounds"] == 2
    assert seen == ["alpha", "beta", "gamma", "alpha", "beta", "gamma"]
    assert result["passed"] == ["gamma"]
    assert [item["bot_id"] for item in result["replies"]] == [
        "alpha",
        "beta",
        "alpha",
        "beta",
    ]


def test_group_size_limit(client, lorca_env):
    """A group has 1..6 members; a 7th is refused."""

    client.get("/api/v1/bots")
    registry = BotRegistry()
    bot_ids = [f"bot{index}" for index in range(MAX_GROUP_MEMBERS + 1)]
    _register(registry, *bot_ids)

    too_many = client.post("/api/v1/bots/groups", json={"name": "Crowd", "bot_ids": bot_ids})
    assert too_many.status_code == 422
    assert "at most 6" in too_many.json()["detail"]

    created = client.post(
        "/api/v1/bots/groups", json={"name": "Team", "bot_ids": bot_ids[:MAX_GROUP_MEMBERS]}
    )
    assert created.status_code == 200
    assert len(created.json()["group"]["bot_ids"]) == MAX_GROUP_MEMBERS

    duplicate = client.post(
        "/api/v1/bots/groups", json={"name": "Dup", "bot_ids": ["bot0", "bot0"]}
    )
    assert duplicate.status_code == 422

    unknown_member = client.post(
        "/api/v1/bots/groups", json={"name": "Ghost", "bot_ids": ["bot0", "ghost"]}
    )
    assert unknown_member.status_code == 422

    empty_name = client.post("/api/v1/bots/groups", json={"name": "", "bot_ids": ["bot0"]})
    assert empty_name.status_code == 422


@pytest.mark.asyncio
async def test_group_message_endpoint_excludes_hidden(client, lorca_env, fake_master, monkeypatch):
    """The group transcript endpoint never returns a PASS reply."""

    client.get("/api/v1/bots")
    _register(BotRegistry(), "alpha", "beta")

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=PASS_SENTINEL if bot_id == "beta" else f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)

    created = client.post(
        "/api/v1/bots/groups", json={"name": "Room", "bot_ids": ["alpha", "beta"]}
    )
    group_id = created.json()["group"]["id"]
    posted = client.post(
        f"/api/v1/bots/groups/{group_id}/messages", json={"text": "anything to add?"}
    )

    assert posted.status_code == 200
    assert [item["bot_id"] for item in posted.json()["replies"]] == ["alpha"]
    assert posted.json()["passed"] == ["beta"]

    messages = client.get(f"/api/v1/bots/groups/{group_id}/messages")
    assert [item["text"] for item in messages.json()["messages"]] == [
        "anything to add?",
        "say:alpha",
    ]
    assert all(not item["hidden"] for item in messages.json()["messages"])

    listed = client.get("/api/v1/bots/groups")
    assert [group["id"] for group in listed.json()["groups"]] == [group_id]
    assert client.get("/api/v1/bots/groups/nope/messages").status_code == 404
    assert client.post("/api/v1/bots/groups/nope/messages", json={"text": "x"}).status_code == 404


def test_group_rounds_default_and_survive_reload(client, lorca_env):
    """One round per message by default; the setting is persisted."""

    client.get("/api/v1/bots")
    _register(BotRegistry(), "alpha", "beta")
    created = client.post("/api/v1/bots/groups", json={"name": "Room", "bot_ids": ["alpha"]})
    assert created.json()["group"]["rounds_per_message"] == 1

    loud = client.post(
        "/api/v1/bots/groups",
        json={"name": "Loud", "bot_ids": ["alpha", "beta"], "rounds_per_message": 3},
    )
    assert loud.json()["group"]["rounds_per_message"] == 3
    reloaded = GroupStore(lorca_env["root"]).get(loud.json()["group"]["id"])
    assert reloaded is not None
    assert reloaded.rounds_per_message == 3
    assert reloaded.bot_ids == ("alpha", "beta")

    clamped = client.post(
        "/api/v1/bots/groups",
        json={"name": "Clamped", "bot_ids": ["alpha"], "rounds_per_message": 99},
    )
    assert clamped.status_code == 422


async def _run_group(*, group: Any, registry: BotRegistry, store: GroupStore, text: str) -> Any:
    from server.bot_team.groups import run_group_turn

    return await run_group_turn(
        group=group,
        text=text,
        user=USER,
        registry=registry,
        conversations=ConversationStore(store.root),
    )
