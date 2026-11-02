"""Attention items with tappable options (options-card method, adapted).

An option is one tappable answer: picking it delivers the option text as
the answer, exactly like typing it. Options ride on the item itself, so
both render surfaces (chat cards, inbox page) get them from one field.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.attention import AttentionStore
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes.bot_team import router as bots_router

USER = {"user_id": "opt-user", "username": "opt"}


def _register_alpha() -> None:
    BotRegistry().register(
        BotSpec(
            bot_id="alpha",
            owner_id=USER["user_id"],
            name="Alpha",
            description="Does alpha work.",
            instructions="You only do alpha work.",
        )
    )


@pytest.fixture
def opt_env(tmp_path, monkeypatch):
    bot_root = tmp_path / "bots-root"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    return {"root": bot_root}


@pytest.fixture
def client():
    app = FastAPI()
    from server import auth as _opt_auth

    app.dependency_overrides[_opt_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def test_options_round_trip(opt_env):
    store = AttentionStore(opt_env["root"])
    item = store.create(
        question="Which one?", bot_id="alpha", options=["Lake hotel", "City hotel"]
    )
    assert item["options"] == ["Lake hotel", "City hotel"]
    assert store.get(item["id"])["options"] == ["Lake hotel", "City hotel"]


def test_options_default_none_and_legacy_items(opt_env):
    store = AttentionStore(opt_env["root"])
    item = store.create(question="Plain?", bot_id="alpha")
    assert item["options"] is None
    # An item written before options existed has no key; listing still works.
    raw = store._load()
    del raw[0]["options"]
    store._save(raw)
    assert store.list(status="open")[0].get("options") is None


def test_options_deduped_and_stripped(opt_env):
    store = AttentionStore(opt_env["root"])
    item = store.create(
        question="Pick?", bot_id="alpha", options=["  A  ", "A", "B"]
    )
    assert item["options"] == ["A", "B"]


def test_options_refused_whole(opt_env):
    store = AttentionStore(opt_env["root"])
    for bad in (
        "not-a-list",
        ["ok", ""],
        ["ok", 3],
        ["x" * 201],
        ["o1", "o2", "o3", "o4", "o5", "o6", "o7"],
    ):
        with pytest.raises(ValueError):
            store.create(question="Pick?", bot_id="alpha", options=bad)
    assert store.list() == []


def test_create_endpoint_accepts_options(opt_env, client):
    _register_alpha()
    resp = client.post(
        "/api/v1/bots/attention",
        json={
            "question": "Which one?",
            "bot_id": "alpha",
            "options": ["Lake hotel", "City hotel"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["item"]["options"] == ["Lake hotel", "City hotel"]


def test_create_endpoint_refuses_bad_options(opt_env, client):
    _register_alpha()
    resp = client.post(
        "/api/v1/bots/attention",
        json={
            "question": "Which one?",
            "bot_id": "alpha",
            "options": ["o1", "o2", "o3", "o4", "o5", "o6", "o7"],
        },
    )
    assert resp.status_code == 422


def test_option_text_answers_like_typed_text(opt_env, client):
    _register_alpha()
    created = client.post(
        "/api/v1/bots/attention",
        json={
            "question": "Which one?",
            "bot_id": "alpha",
            "options": ["Lake hotel", "City hotel"],
        },
    ).json()["item"]
    resp = client.post(
        f"/api/v1/bots/attention/{created['id']}/answer", json={"answer": "Lake hotel"}
    )
    assert resp.status_code == 200
    assert resp.json()["item"]["answer"] == "Lake hotel"
    assert resp.json()["item"]["status"] == "answered"
