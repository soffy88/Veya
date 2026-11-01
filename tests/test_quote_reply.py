"""Quote replies: server-built snapshots, same-transcript only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore, run_group_turn
from server.bot_team.quote import QUOTE_MAX_CHARS, build_quote
from server.events import EventStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.goal_run.models import GoalRunResponse, GoalStatus
from server.routes import product as product_routes
from server.routes.bot_team import router as bots_router
from server.task_store import TaskStore

USER = {"user_id": "quote-user", "username": "quote"}


@pytest.fixture
def quote_env(tmp_path, monkeypatch):
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
    async def fake_execute(request: Any) -> GoalRunResponse:
        bot_id = str(request.constraints.metadata.get("bot_id") or "")
        return GoalRunResponse(
            goal_id=f"goal-{bot_id}",
            status=GoalStatus.completed,
            phase="finalized",
            summary=f"say:{bot_id}",
        )

    from server.coordinator_master import master_coordinator

    monkeypatch.setattr(master_coordinator, "execute_structured", fake_execute)


@pytest.fixture
def client():
    app = FastAPI()
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def _register(registry: BotRegistry, specs: list[tuple[str, str, str]]) -> BotRegistry:
    for bot_id, description, instructions in specs:
        registry.register(
            BotSpec(
                bot_id=bot_id,
                owner_id=USER["user_id"],
                name=bot_id.capitalize(),
                description=description,
                instructions=instructions,
            )
        )
    return registry


def test_build_quote_found_and_capped():
    messages = [
        {"id": "m1", "role": "user", "text": "hello", "hidden": False},
        {"id": "m2", "role": "bot", "bot_id": "chef", "text": "x" * 600},
    ]
    quote = build_quote(messages, "m1")
    assert quote == {"id": "m1", "author": "user", "text": "hello"}
    long_quote = build_quote(messages, "m2")
    assert long_quote["author"] == "chef"
    assert len(long_quote["text"]) == QUOTE_MAX_CHARS + 1  # trailing ellipsis
    assert long_quote["text"].endswith("…")


def test_build_quote_unknown_and_hidden():
    messages = [{"id": "m1", "role": "bot", "bot_id": "chef", "text": "[PASS]", "hidden": True}]
    with pytest.raises(ValueError, match="not found"):
        build_quote(messages, "nope")
    with pytest.raises(ValueError, match="hidden"):
        build_quote(messages, "m1")


def test_append_message_stores_reply_to(quote_env):
    store = GroupStore(quote_env["root"])
    group = store.create(name="room", bot_ids=["chef"], admin_bot_id="chef")
    first = store.append_message(group.id, role="user", text="first")
    assert "reply_to" not in first
    quote = build_quote(store.messages(group.id, include_hidden=True), first["id"])
    second = store.append_message(
        group.id, role="user", text="second", reply_to=quote
    )
    assert second["reply_to"] == quote


async def test_group_turn_records_quote(quote_env, fake_master):
    registry = _register(
        BotRegistry(),
        [("chef", "Cooks dinner.", "You cook dinner.")],
    )
    store = GroupStore(quote_env["root"])
    group = store.create(
        name="room", bot_ids=["chef"], admin_bot_id="chef", reply_mode="everyone"
    )
    first = store.append_message(group.id, role="user", text="what is for dinner?")
    quote = build_quote(store.messages(group.id, include_hidden=True), first["id"])
    await run_group_turn(
        group=group,
        text="second question",
        user=dict(USER),
        registry=registry,
        conversations=ConversationStore(quote_env["root"]),
        reply_to=quote,
    )
    transcript = store.messages(group.id, include_hidden=True)
    user_posts = [m for m in transcript if m["role"] == "user"]
    assert user_posts[-1]["reply_to"] == quote


def test_group_message_api_quote_and_422(quote_env, fake_master, client):
    _register(BotRegistry(), [("chef", "Cooks dinner.", "You cook dinner.")])
    store = GroupStore(quote_env["root"])
    group = store.create(
        name="room", bot_ids=["chef"], admin_bot_id="chef", reply_mode="everyone"
    )
    first = store.append_message(group.id, role="user", text="first question")
    resp = client.post(
        f"/api/v1/bots/groups/{group.id}/messages",
        json={"text": "second question", "reply_to": first["id"]},
    )
    assert resp.status_code == 200, resp.text
    transcript = client.get(f"/api/v1/bots/groups/{group.id}/messages").json()["messages"]
    quoted = [m for m in transcript if m.get("reply_to")]
    assert len(quoted) == 1
    assert quoted[0]["reply_to"]["text"] == "first question"

    bad = client.post(
        f"/api/v1/bots/groups/{group.id}/messages",
        json={"text": "third", "reply_to": "missing-id"},
    )
    assert bad.status_code == 422


def test_bot_message_api_quote_in_meta(quote_env, fake_master, client):
    _register(BotRegistry(), [("chef", "Cooks dinner.", "You cook dinner.")])
    first = client.post("/api/v1/bots/chef/messages", json={"text": "first question"})
    assert first.status_code == 200, first.text
    first_id = client.get("/api/v1/bots/chef/messages").json()["messages"][0]["id"]
    second = client.post(
        "/api/v1/bots/chef/messages", json={"text": "follow up", "reply_to": first_id}
    )
    assert second.status_code == 200, second.text
    messages = client.get("/api/v1/bots/chef/messages").json()["messages"]
    quoted = [m for m in messages if (m.get("meta") or {}).get("reply_to")]
    assert len(quoted) == 1
    assert quoted[0]["meta"]["reply_to"]["text"] == "first question"

    bad = client.post("/api/v1/bots/chef/messages", json={"text": "x", "reply_to": "missing-id"})
    assert bad.status_code == 422
