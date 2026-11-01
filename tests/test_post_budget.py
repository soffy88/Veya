"""Room post budget: duplicate / ring / sender-rate / escalate."""

from __future__ import annotations

from typing import Any

import pytest

from server.bot_team.attention import AttentionStore
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore, run_group_turn
from server.bot_team.post_budget import RoomPostBudget, reset_room_budgets
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.task_store import TaskStore

USER = {"user_id": "budget-user", "username": "budget"}


@pytest.fixture(autouse=True)
def _reset_budgets():
    reset_room_budgets()
    yield
    reset_room_budgets()


def test_duplicate_within_window_refused_then_allowed():
    budget = RoomPostBudget(now=lambda: 1000.0)
    assert budget.decide("alpha", "same text").allowed
    assert budget.decide("alpha", "same text").reason == "duplicate"
    assert budget.decide("beta", "same text").allowed
    budget2 = RoomPostBudget(now=lambda: 1000.0 + 61.0)
    assert budget2.decide("alpha", "same text").allowed


def test_ring_trips_breaker_and_user_message_lifts_it():
    budget = RoomPostBudget(now=lambda: 1000.0)
    assert budget.decide("alpha", "one").allowed
    assert budget.decide("beta", "one").allowed
    assert budget.decide("alpha", "two").allowed
    refused = budget.decide("beta", "two")
    assert refused.reason == "ring" and not refused.allowed
    assert budget.decide("alpha", "three").reason == "breaker"
    budget.note_user_message(now=1000.0)
    assert budget.decide("alpha", "three").allowed


def test_sender_rate_caps_at_ten():
    budget = RoomPostBudget(now=lambda: 1000.0)
    for i in range(10):
        assert budget.decide("alpha", f"post {i}").allowed
    assert budget.decide("alpha", "post 10").reason == "sender-rate"


def test_escalate_on_second_unanswered_solo_post():
    budget = RoomPostBudget(now=lambda: 1000.0)
    budget.note_user_message(now=1000.0)
    assert budget.decide("alpha", "solo one").allowed
    refused = budget.decide("alpha", "solo two")
    assert refused.escalate and refused.reason == "escalate"
    # A refusal is never recorded: the next identical post is still only
    # held by the breaker, not by a duplicate.
    assert budget.decide("alpha", "solo two").reason == "breaker"


def test_attention_kind_validated(tmp_path):
    store = AttentionStore(tmp_path)
    with pytest.raises(ValueError):
        store.create(question="x", kind="bogus")
    item = store.create(question="x", kind="incident")
    assert item["kind"] == "incident"


@pytest.fixture
def budget_env(tmp_path, monkeypatch):
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


def _register(*bot_ids: str) -> BotRegistry:
    registry = BotRegistry()
    for bot_id in bot_ids:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=f"{bot_id} does {bot_id} work.",
                instructions=f"You are {bot_id}.",
            )
        )
    return registry


def _fake_master(monkeypatch, scripts: dict[str, list[str]]):
    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        queue = scripts.get(bot_id, [])
        summary = queue.pop(0) if queue else f"say:{bot_id}"
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=summary,
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)


async def _run(env, registry, group, text):
    return await run_group_turn(
        group=group,
        text=text,
        user=dict(USER),
        registry=registry,
        conversations=ConversationStore(env["root"]),
    )


@pytest.mark.asyncio
async def test_ring_stops_ping_pong_round(budget_env, monkeypatch):
    registry = _register("alpha", "beta")
    _fake_master(
        monkeypatch,
        {
            "alpha": ["@beta one", "@beta two"],
            "beta": ["@alpha one", "@alpha two"],
        },
    )
    store = GroupStore(budget_env["root"])
    group = store.create(
        name="pingpong",
        bot_ids=["alpha", "beta"],
        admin_bot_id="alpha",
        reply_mode="everyone",
        rounds_per_message=5,
    )
    result = await _run(budget_env, registry, group, "go")
    assert [r["bot_id"] for r in result["replies"]] == ["alpha", "beta", "alpha"]
    assert result["blocked"] == ["beta"]
    visible = [
        m for m in store.messages(group.id) if m.get("role") == "bot"
    ]
    assert len(visible) == 3


@pytest.mark.asyncio
async def test_escalate_creates_attention_item(budget_env, monkeypatch):
    registry = _register("alpha")
    _fake_master(monkeypatch, {"alpha": ["@alpha solo one", "@alpha solo two"]})
    store = GroupStore(budget_env["root"])
    group = store.create(
        name="solo",
        bot_ids=["alpha"],
        admin_bot_id="alpha",
        reply_mode="everyone",
        rounds_per_message=5,
    )
    result = await _run(budget_env, registry, group, "go")
    assert len(result["replies"]) == 1
    assert result["blocked"] == ["alpha"]
    items = AttentionStore(budget_env["root"]).list(status="open")
    assert len(items) == 1
    assert items[0]["kind"] == "escalation"
    assert items[0]["group_id"] == group.id
