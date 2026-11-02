"""Tell policy (item 2): only the eight decision categories may interrupt
the owner; everything else defaults to silent; explicit tell wins."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team import attention as att_mod
from server.bot_team.attention import AttentionStore, resolve_tell, tell_for
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes.bot_team import router as bots_router

USER = {"user_id": "tell-user", "username": "tell"}


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


# ── resolution ────────────────────────────────────────────────────────


def test_resolve_tell_precedence():
    assert resolve_tell() == "silent"
    assert resolve_tell(category="delete-data") == "needed"
    assert resolve_tell(category="delete-data", tell="silent") == "silent"
    assert resolve_tell(tell="always") == "always"
    assert resolve_tell(tell="DIGEST") == "digest"


def test_resolve_tell_validation():
    with pytest.raises(ValueError, match="category"):
        resolve_tell(category="whatever")
    with pytest.raises(ValueError, match="tell"):
        resolve_tell(tell="loud")


def test_tell_for_legacy_items():
    assert tell_for({}) == "needed"
    assert tell_for({"category": "spend-money"}) == "needed"
    assert tell_for({"category": "spend-money", "tell": "silent"}) == "silent"
    assert tell_for({"tell": "digest"}) == "digest"


# ── store ─────────────────────────────────────────────────────────────


def test_create_stores_category_and_tell(att_env):
    store = AttentionStore(att_env["root"])
    item = store.create(
        question="删吗？", bot_id="alpha", category="delete-data"
    )
    assert item["category"] == "delete-data"
    assert item["tell"] == "needed"
    plain = store.create(question="今晚吃什么？", bot_id="alpha")
    assert plain["category"] is None
    assert plain["tell"] == "silent"


def test_create_unknown_category_or_tell_refuses(att_env):
    store = AttentionStore(att_env["root"])
    with pytest.raises(ValueError, match="category"):
        store.create(question="Q?", bot_id="alpha", category="nope")
    with pytest.raises(ValueError, match="tell"):
        store.create(question="Q?", bot_id="alpha", tell="loud")
    assert store.list(status="open") == []


def test_silent_records_digest_only_when_flag_on(att_env, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(att_mod, "_record_tell_digest", calls.append)
    store = AttentionStore(att_env["root"])
    monkeypatch.setenv("VEYA_TELL_POLICY", "1")
    item = store.create(question="今晚吃什么？", bot_id="alpha")
    assert item["tell"] == "silent"
    assert [c["id"] for c in calls] == [item["id"]]
    calls.clear()
    monkeypatch.setenv("VEYA_TELL_POLICY", "0")
    store.create(question="明天吃什么？", bot_id="alpha")
    assert calls == []


def test_needed_does_not_record_digest(att_env, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(att_mod, "_record_tell_digest", calls.append)
    monkeypatch.setenv("VEYA_TELL_POLICY", "1")
    store = AttentionStore(att_env["root"])
    store.create(question="删吗？", bot_id="alpha", category="delete-data")
    assert calls == []


# ── routes ────────────────────────────────────────────────────────────


def test_route_category_and_tell(client, att_env):
    _register(BotRegistry(), "alpha")
    r = client.post(
        "/api/v1/bots/attention",
        json={"question": "删吗？", "bot_id": "alpha", "category": "delete-data"},
    )
    assert r.status_code == 200
    assert r.json()["item"]["tell"] == "needed"
    r2 = client.post(
        "/api/v1/bots/attention",
        json={
            "question": "删吗？",
            "bot_id": "alpha",
            "category": "delete-data",
            "tell": "digest",
        },
    )
    assert r2.json()["item"]["tell"] == "digest"


def test_route_unknown_category_is_422(client, att_env):
    _register(BotRegistry(), "alpha")
    r = client.post(
        "/api/v1/bots/attention",
        json={"question": "Q?", "bot_id": "alpha", "category": "nope"},
    )
    assert r.status_code == 422
    assert client.get("/api/v1/bots/attention").json()["attention"] == []
