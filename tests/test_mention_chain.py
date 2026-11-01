"""Mention-chain continuation: later rounds call only newly @-mentioned members."""

from __future__ import annotations

from typing import Any

import pytest

from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore, mentioned_bot_ids, run_group_turn
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.task_store import TaskStore

USER = {"user_id": "chain-user", "username": "chain"}


@pytest.fixture
def chain_env(tmp_path, monkeypatch):
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


def _register(registry: BotRegistry, *bot_ids: str) -> BotRegistry:
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


def _fake_master(monkeypatch, replies: dict[str, str], seen: list[str]):
    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        seen.append(bot_id)
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=replies.get(bot_id, f"say:{bot_id}"),
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)


async def _run(chain_env, registry, group, text):
    return await run_group_turn(
        group=group,
        text=text,
        user=dict(USER),
        registry=registry,
        conversations=ConversationStore(chain_env["root"]),
    )


def test_mentioned_bot_ids_bounded():
    registry = _register(BotRegistry(), "alpha", "beta")
    assert mentioned_bot_ids(["@beta what do you think?"], ["alpha", "beta"], registry) == ["beta"]
    assert mentioned_bot_ids(["@beta2 hello"], ["alpha", "beta"], registry) == []
    assert mentioned_bot_ids(["mail beta@example.com"], ["alpha", "beta"], registry) == []
    assert mentioned_bot_ids(["@Alpha, over to you"], ["alpha", "beta"], registry) == ["alpha"]
    assert mentioned_bot_ids(["@beta then @alpha"], ["alpha", "beta"], registry) == ["beta", "alpha"]


async def test_chain_calls_only_mentioned(chain_env, monkeypatch):
    registry = _register(BotRegistry(), "alpha", "beta", "gamma")
    store = GroupStore(chain_env["root"])
    group = store.create(
        name="Chain", bot_ids=["alpha", "beta", "gamma"], rounds_per_message=3
    )
    seen: list[str] = []
    _fake_master(
        monkeypatch,
        {"alpha": "@gamma, your turn", "beta": "nothing to add", "gamma": "done"},
        seen,
    )
    result = await _run(chain_env, registry, group, "ideas?")
    # Round 1: all three. Round 2: gamma only (mentioned, unspoken).
    # Gamma mentions nobody new, so no round 3.
    assert seen == ["alpha", "beta", "gamma", "gamma"]
    assert result["rounds"] == 2
    assert [r["bot_id"] for r in result["replies"]] == ["alpha", "beta", "gamma", "gamma"]


async def test_no_mentions_stops_after_round_one(chain_env, monkeypatch):
    registry = _register(BotRegistry(), "alpha", "beta", "gamma")
    store = GroupStore(chain_env["root"])
    group = store.create(
        name="Quiet", bot_ids=["alpha", "beta", "gamma"], rounds_per_message=3
    )
    seen: list[str] = []
    _fake_master(monkeypatch, {}, seen)
    result = await _run(chain_env, registry, group, "ideas?")
    assert seen == ["alpha", "beta", "gamma"]
    assert result["rounds"] == 1


async def test_round_two_opener_rotates(chain_env, monkeypatch):
    registry = _register(BotRegistry(), "alpha", "beta", "gamma")
    store = GroupStore(chain_env["root"])
    group = store.create(
        name="Rotate", bot_ids=["alpha", "beta", "gamma"], rounds_per_message=2
    )
    seen: list[str] = []
    _fake_master(
        monkeypatch,
        {"alpha": "@beta @gamma, both of you", "beta": "ok", "gamma": "ok"},
        seen,
    )
    result = await _run(chain_env, registry, group, "ideas?")
    # Round 1 order: alpha, beta, gamma. Round 2 candidates [beta, gamma]
    # rotate by 1 -> gamma opens.
    assert seen == ["alpha", "beta", "gamma", "gamma", "beta"]
    assert result["rounds"] == 2


async def test_passed_member_never_recalled_even_if_mentioned(chain_env, monkeypatch):
    from server.bot_team.conversations import PASS_SENTINEL

    registry = _register(BotRegistry(), "alpha", "beta", "gamma")
    store = GroupStore(chain_env["root"])
    group = store.create(
        name="Pass", bot_ids=["alpha", "beta", "gamma"], rounds_per_message=3
    )
    seen: list[str] = []
    _fake_master(
        monkeypatch,
        {"alpha": "@gamma, anything?", "beta": "no", "gamma": PASS_SENTINEL},
        seen,
    )
    result = await _run(chain_env, registry, group, "ideas?")
    assert seen == ["alpha", "beta", "gamma"]
    assert result["passed"] == ["gamma"]
    assert result["rounds"] == 1
