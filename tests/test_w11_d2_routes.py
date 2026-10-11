"""W11 D2: bot and group routes stay private to the owner."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import auth
from server.goal_run.bot_identity import BotRegistry, BotSpec
from server.routes.bot_team import router as bots_router

ALICE = {"user_id": "alice", "username": "alice"}
BOB = {"user_id": "bob", "username": "bob"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(tmp_path / "bot_registry.json"))
    who = {"user": ALICE}
    app = FastAPI()
    app.dependency_overrides[auth.require_user] = lambda: dict(who["user"])
    app.include_router(bots_router)
    BotRegistry().register(BotSpec(bot_id="alice_bot", owner_id="alice", name="A"))
    return TestClient(app), who


def test_other_user_cannot_read_delete_or_group_alice_bot(env):
    client, who = env
    created = client.post("/api/v1/bots/groups", json={"name": "g", "bot_ids": ["alice_bot"]})
    assert created.status_code == 200, created.text
    gid = created.json()["group"]["id"]

    who["user"] = BOB
    assert client.get("/api/v1/bots/alice_bot/messages").status_code == 404
    assert client.delete("/api/v1/bots/alice_bot").status_code == 404
    assert client.get("/api/v1/bots/groups").json()["groups"] == []
    assert client.get(f"/api/v1/bots/groups/{gid}/messages").status_code == 404
    assert client.post(f"/api/v1/bots/groups/{gid}/messages", json={"text": "hi"}).status_code == 404
    assert client.post("/api/v1/bots/groups", json={"name": "x", "bot_ids": ["alice_bot"]}).status_code == 422

    who["user"] = ALICE
    assert client.get("/api/v1/bots/alice_bot/messages").status_code == 200
    assert [g["id"] for g in client.get("/api/v1/bots/groups").json()["groups"]] == [gid]
    assert client.delete("/api/v1/bots/alice_bot").status_code == 200


def test_anonymous_is_refused():
    app = FastAPI()
    app.include_router(bots_router)
    c = TestClient(app)
    assert c.get("/api/v1/bots/x/messages").status_code == 401
    assert c.delete("/api/v1/bots/x").status_code == 401
    assert c.get("/api/v1/bots/groups").status_code == 401


def test_routines_are_private_to_the_bot_owner(env, tmp_path, monkeypatch):
    from server.routes.routines import router as routines_router

    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    client, who = env
    client.app.include_router(routines_router)
    who["user"] = BOB
    assert client.get("/api/v1/bots/alice_bot/routines").status_code == 404
    assert client.post("/api/v1/bots/alice_bot/routines", json={"objective": "x", "schedule": "every 5m"}).status_code == 404
    who["user"] = ALICE
    assert client.post("/api/v1/bots/alice_bot/routines", json={"objective": "x", "schedule": "every 5m"}).status_code == 200
