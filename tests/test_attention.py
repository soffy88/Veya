"""Attention queue, status board and inbox (Soulgent method, adapted).

Needs-you covers approvals; this covers questions a bot or a room asks
the owner. An answered item is delivered as a user message, so the bot
sees it on its next turn; a dismissed item closes without delivery.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.attention import AttentionStore, build_board
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes.bot_team import router as bots_router

USER = {"user_id": "att-user", "username": "att"}


@pytest.fixture
def att_env(tmp_path, monkeypatch):
    bot_root = tmp_path / "bots-root"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    return {"root": bot_root}


@pytest.fixture
def client():
    app = FastAPI()
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


def test_attention_store_crud(att_env):
    store = AttentionStore(att_env["root"])
    item = store.create(question="Which hotel?", bot_id="alpha")
    assert item["status"] == "open"
    assert store.list(status="open") == [item]
    assert store.get(item["id"]) == item
    assert store.answer("nope", "x") is None
    closed = store.answer(item["id"], "The one near the lake")
    assert closed is not None and closed["status"] == "answered"
    assert closed["answer"] == "The one near the lake"
    assert store.answer(item["id"], "again") is None
    assert store.dismiss(item["id"]) is None
    second = store.create(question="Again?", bot_id="alpha")
    assert store.dismiss(second["id"]) is not None
    assert store.list(status="open") == []


def test_attention_create_validation_api(client, att_env):
    _register(BotRegistry(), "alpha")
    assert client.post("/api/v1/bots/attention", json={"question": "  "}).status_code == 422
    assert client.post("/api/v1/bots/attention", json={"question": "Q?"}).status_code == 422
    assert (
        client.post(
            "/api/v1/bots/attention", json={"question": "Q?", "bot_id": "ghost"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/bots/attention", json={"question": "Q?", "group_id": "grp_nope"}
        ).status_code
        == 404
    )
    created = client.post(
        "/api/v1/bots/attention", json={"question": "Which hotel?", "bot_id": "alpha"}
    )
    assert created.status_code == 200, created.text
    assert created.json()["item"]["status"] == "open"
    listed = client.get("/api/v1/bots/attention")
    assert len(listed.json()["attention"]) == 1


def test_attention_answer_delivers_to_bot(client, att_env):
    _register(BotRegistry(), "alpha")
    created = client.post(
        "/api/v1/bots/attention", json={"question": "Which hotel?", "bot_id": "alpha"}
    )
    item_id = created.json()["item"]["id"]
    answered = client.post(
        f"/api/v1/bots/attention/{item_id}/answer", json={"answer": "The lake one"}
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["item"]["status"] == "answered"
    messages = ConversationStore(att_env["root"]).list("alpha")
    assert messages[-1]["role"] == "user" and messages[-1]["text"] == "The lake one"
    assert client.post(
        f"/api/v1/bots/attention/{item_id}/answer", json={"answer": "again"}
    ).status_code == 409
    assert client.post(f"/api/v1/bots/attention/{item_id}/dismiss").status_code == 409


def test_attention_answer_delivers_to_group(client, att_env):
    _register(BotRegistry(), "alpha", "beta")
    group = GroupStore(att_env["root"]).create(
        name="room", bot_ids=["alpha", "beta"], admin_bot_id="alpha"
    )
    created = client.post(
        "/api/v1/bots/attention",
        json={"question": "Where to?", "group_id": group.id},
    )
    assert created.status_code == 200, created.text
    item_id = created.json()["item"]["id"]
    answered = client.post(
        f"/api/v1/bots/attention/{item_id}/answer", json={"answer": "Hangzhou"}
    )
    assert answered.status_code == 200, answered.text
    transcript = GroupStore(att_env["root"]).messages(group.id)
    assert transcript[-1]["role"] == "user" and transcript[-1]["text"] == "Hangzhou"


def test_attention_dismiss_api(client, att_env):
    _register(BotRegistry(), "alpha")
    created = client.post(
        "/api/v1/bots/attention", json={"question": "Q?", "bot_id": "alpha"}
    )
    item_id = created.json()["item"]["id"]
    dismissed = client.post(f"/api/v1/bots/attention/{item_id}/dismiss")
    assert dismissed.status_code == 200
    assert dismissed.json()["item"]["status"] == "dismissed"
    assert client.get("/api/v1/bots/attention").json()["attention"] == []


def test_board_statuses(att_env):
    registry = _register(BotRegistry(), "alpha", "beta", "gamma")
    conversations = ConversationStore(att_env["root"])
    store = AttentionStore(att_env["root"])
    conversations.append("beta", role="user", text="do the thing")
    conversations.append("gamma", role="bot", text="done")
    store.create(question="Which?", bot_id="alpha")
    board = {
        row["bot_id"]: row
        for row in build_board(
            registry, conversations=conversations, store=store, owner_id=USER["user_id"]
        )
    }
    assert board["alpha"]["status"] == "waiting"
    assert board["alpha"]["open_attention"] == 1
    assert board["beta"]["status"] == "working"
    assert board["gamma"]["status"] == "idle"
    assert board["gamma"]["last_active_ts"] is not None


def test_inbox_shape(client, att_env):
    _register(BotRegistry(), "alpha", "beta")
    group = GroupStore(att_env["root"]).create(
        name="room", bot_ids=["alpha", "beta"], admin_bot_id="alpha"
    )
    client.post(
        "/api/v1/bots/attention", json={"question": "Q?", "bot_id": "alpha"}
    )
    inbox = client.get("/api/v1/bots/inbox")
    assert inbox.status_code == 200, inbox.text
    data = inbox.json()
    assert len(data["attention"]) == 1
    assert {row["bot_id"] for row in data["board"]} >= {"alpha", "beta"}
    assert [g["group_id"] for g in data["groups"]] == [group.id]
    assert data["groups"][0]["status"] == "idle"
    board = client.get("/api/v1/bots/board")
    assert board.status_code == 200
    assert {row["bot_id"] for row in board.json()["board"]} >= {"alpha", "beta"}
