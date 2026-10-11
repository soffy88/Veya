"""W11 D2: bot-runtime routes are private to the bot's owner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth as auth_mod
from server.routes import bot_runtime as routes


class _Registry:
    def __init__(self) -> None:
        self.items: dict[str, Any] = {}

    def get(self, bot_id: str) -> Any:
        return self.items.get(bot_id)


class _Handle:
    def __init__(self, bot_id: str) -> None:
        self.bot_id = bot_id

    def to_dict(self) -> dict[str, Any]:
        return {"bot_id": self.bot_id, "status": "ready"}


class _Manager:
    def __init__(self, registry: _Registry) -> None:
        self.registry = registry
        self.calls: list[str] = []

    def create_bot(self, *, bot_id: str, owner_id: str, **_: Any) -> _Handle:
        self.registry.items[bot_id] = SimpleNamespace(bot_id=bot_id, owner_id=owner_id)
        return _Handle(bot_id)

    def list_bots(self, owner_id: str | None = None) -> list[_Handle]:
        return [_Handle(b) for b, s in self.registry.items.items() if s.owner_id == owner_id]

    def get_bot(self, bot_id: str) -> _Handle:
        return _Handle(bot_id)

    async def stop_bot(self, bot_id: str) -> None:
        self.calls.append(f"stop:{bot_id}")


@pytest.fixture
def env(monkeypatch):
    registry = _Registry()
    manager = _Manager(registry)
    monkeypatch.setattr(routes, "_manager", lambda: (manager, registry))
    app = FastAPI()
    app.include_router(routes.router)
    who = {"user_id": "alice"}
    app.dependency_overrides[auth_mod.require_user] = lambda: dict(who)
    return TestClient(app), who, manager


def _create(client: TestClient, bot_id: str = "bot-a", **extra: Any):
    return client.post("/api/v1/bot-runtime", json={"bot_id": bot_id, "runtime_id": "agent_loop", **extra})


def test_owner_comes_from_session_not_body(env):
    client, _who, manager = env
    assert _create(client).status_code == 200
    assert manager.registry.items["bot-a"].owner_id == "alice"
    assert _create(client, "bot-b", owner_id="mallory").status_code == 403


def test_other_user_gets_404_on_every_bot_route(env):
    client, who, manager = env
    _create(client)
    who["user_id"] = "mallory"
    base = "/api/v1/bot-runtime/bot-a"
    assert client.get(base).status_code == 404
    assert client.post(f"{base}/stop").status_code == 404
    assert client.post(f"{base}/deploy", json={}).status_code == 404
    assert client.post(f"{base}/messages", json={"message": "hi"}).status_code == 404
    assert client.get(f"{base}/sessions").status_code == 404
    assert client.get(f"{base}/goals/g1").status_code == 404
    assert manager.calls == []


def test_list_shows_only_own_bots_and_ids_cannot_be_hijacked(env):
    client, who, _manager = env
    _create(client)
    who["user_id"] = "mallory"
    assert client.get("/api/v1/bot-runtime").json() == []
    assert _create(client, "bot-a").status_code == 409


def test_owner_can_act_on_own_bot(env):
    client, _who, manager = env
    _create(client)
    assert client.get("/api/v1/bot-runtime/bot-a").status_code == 200
    assert client.post("/api/v1/bot-runtime/bot-a/stop").status_code == 200
    assert manager.calls == ["stop:bot-a"]
