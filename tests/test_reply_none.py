"""reply=none fire-and-forget, consult/done_when/rework, bounded retries."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.bot_team.attention import AttentionStore
from server.bot_team.conversations import ConversationStore
from server.bot_team.delegations import DelegationStore, process_delegations
from server.bot_team.messaging import BotMessenger
from server.bot_team.tool import delegate_bot, message_bot
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec, bind_current_bot, reset_current_bot
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.task_store import TaskStore

USER = {"user_id": "reply-user", "username": "reply"}


@pytest.fixture
def reply_env(tmp_path, monkeypatch):
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


@pytest.mark.asyncio
async def test_reply_none_runs_target_but_no_follow_up(reply_env, monkeypatch):
    registry = _register("alpha", "beta")
    seen: list[str] = []
    objectives: dict[str, list[str]] = {}

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        seen.append(bot_id)
        objectives.setdefault(bot_id, []).append(str(getattr(request, "objective", "") or ""))
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    store = ConversationStore(reply_env["root"])
    messenger = BotMessenger(registry, conversations=store)
    receipt = await messenger.delegate("alpha", "beta", "tell beta only", depth=0, reply_mode="none")
    assert receipt["reply_mode"] == "none"
    outcomes = await process_delegations(conversations=store, user=dict(USER))
    assert outcomes[0]["status"] == "done"
    assert outcomes[0]["reply_mode"] == "none"
    assert seen == ["beta"]
    alpha_texts = [m.get("text", "") for m in store.list("alpha")]
    assert alpha_texts.count("say:beta") == 1  # the note only, no follow-up turn
    assert "say:alpha" not in alpha_texts


@pytest.mark.asyncio
async def test_reply_mode_validated(reply_env):
    registry = _register("alpha", "beta")
    messenger = BotMessenger(registry)
    with pytest.raises(ValueError):
        await messenger.delegate("alpha", "beta", "x", depth=0, reply_mode="smoke-signal")
    token = bind_current_bot("alpha")
    try:
        with pytest.raises(ValueError):
            await delegate_bot("beta", "x", reply="smoke-signal")
    finally:
        reset_current_bot(token)


@pytest.mark.asyncio
async def test_consult_done_when_rework_reach_target_context(reply_env, monkeypatch):
    registry = _register("alpha", "beta")
    objectives: dict[str, list[str]] = {}

    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        objectives.setdefault(bot_id, []).append(str(getattr(request, "objective", "") or ""))
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary="consulted",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)
    messenger = BotMessenger(registry)
    await messenger.message_bot(
        "alpha",
        "beta",
        "is this feasible?",
        consult=True,
        done_when="a yes/no with one reason",
        rework=True,
    )
    objective = objectives["beta"][0]
    assert "consultation only" in objective
    assert "Done means: a yes/no with one reason" in objective
    assert "rework, not a new request" in objective
    token = bind_current_bot("alpha")
    try:
        receipt = json.loads(
            await delegate_bot(
                "beta", "check this", consult=True, done_when="a verdict", rework=False
            )
        )
    finally:
        reset_current_bot(token)
    item = DelegationStore().get(receipt["delegation_id"])
    assert item["consult"] is True
    assert item["done_when"] == "a verdict"
    assert item["rework"] is False


@pytest.mark.asyncio
async def test_crashing_target_retries_twice_then_incident(reply_env, monkeypatch):
    """Exceptions escaping message_bot are retried twice, then escalated.

    (A model-level failure never reaches here: message_bot degrades it
    to a visible warning reply, unchanged.)
    """

    from server.bot_team.messaging import BotMessenger as _Messenger

    registry = _register("alpha", "beta")

    async def failing_message_bot(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_Messenger, "message_bot", failing_message_bot)
    store = ConversationStore(reply_env["root"])
    messenger = BotMessenger(registry, conversations=store)
    await messenger.delegate("alpha", "beta", "doomed", depth=0)
    first = await process_delegations(conversations=store, user=dict(USER))
    assert first[0]["status"] == "retrying" and first[0]["retries"] == 1
    second = await process_delegations(conversations=store, user=dict(USER))
    assert second[0]["status"] == "retrying" and second[0]["retries"] == 2
    third = await process_delegations(conversations=store, user=dict(USER))
    assert third[0]["status"] == "failed"
    item = DelegationStore(root=reply_env["root"]).list()[0]
    assert item["status"] == "failed" and item["retries"] == 2
    incidents = [
        item for item in AttentionStore(reply_env["root"]).list(status="open")
        if item.get("kind") == "incident"
    ]
    assert len(incidents) == 1
    assert incidents[0]["bot_id"] == "alpha"
