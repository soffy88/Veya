"""Global rev: one counter for every change, sync poll, read marks, unread."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.bot_team.attention import AttentionStore
from server.bot_team.conversations import ConversationStore
from server.bot_team.groups import GroupStore
from server.bot_team.rev import (
    ReadMarkStore,
    bump_rev,
    current_rev,
    unread_for_group,
)
from server.bot_team.routine_holds import RoutineHoldStore
from server.routes.bot_team import router as bots_router

USER = {"user_id": "rev-user", "username": "rev"}


@pytest.fixture
def rev_env(tmp_path, monkeypatch):
    root = tmp_path / "bots-root"
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(root))
    return {"root": root}


@pytest.fixture
def client():
    app = FastAPI()
    from server import auth as _w11_auth

    app.dependency_overrides[_w11_auth.require_user] = lambda: dict(USER)
    app.include_router(bots_router)
    return TestClient(app)


def test_counter_starts_zero_and_increments(rev_env):
    assert current_rev(root=rev_env["root"]) == 0
    assert bump_rev(root=rev_env["root"]) == 1
    assert bump_rev(root=rev_env["root"]) == 2
    assert current_rev(root=rev_env["root"]) == 2


def test_conversation_append_stamps_increasing_revs(rev_env):
    store = ConversationStore(rev_env["root"])
    first = store.append("chef", role="user", text="one")
    second = store.append("chef", role="bot", text="two")
    assert first["rev"] == 1
    assert second["rev"] == 2
    assert current_rev(root=rev_env["root"]) == 2


def test_group_append_stamps_rev(rev_env):
    store = GroupStore(rev_env["root"])
    group = store.create(name="room", bot_ids=["chef"], admin_bot_id="chef")
    first = store.append_message(group.id, role="user", text="one")
    second = store.append_message(group.id, role="bot", bot_id="chef", text="two")
    assert first["rev"] == 1
    assert second["rev"] == 2


def test_attention_and_hold_mutations_bump(rev_env):
    attention = AttentionStore(rev_env["root"])
    item = attention.create(question="q?", bot_id="chef")
    assert current_rev(root=rev_env["root"]) == 1
    attention.answer(item["id"], "a")
    assert current_rev(root=rev_env["root"]) == 2
    attention.dismiss(attention.create(question="q2?", bot_id="chef")["id"])
    assert current_rev(root=rev_env["root"]) == 4

    holds = RoutineHoldStore(rev_env["root"])
    hold = holds.create(bot_id="chef", routine_id="r1", reason="wait")
    assert current_rev(root=rev_env["root"]) == 5
    holds.resolve(hold["id"], status="resumed")
    assert current_rev(root=rev_env["root"]) == 6


def test_read_marks_never_move_backwards(rev_env):
    marks = ReadMarkStore(rev_env["root"])
    assert marks.get("u1", "g1") == 0
    assert marks.mark("u1", "g1", 5) == 5
    assert marks.mark("u1", "g1", 3) == 5
    assert marks.get("u1", "g1") == 5


def test_unread_counts_final_answers_and_cards():
    messages = [
        {"role": "bot", "text": "old", "rev": 1},
        {"role": "bot", "text": "new", "rev": 3},
        {"role": "user", "text": "q", "rev": 4},
        {"role": "bot", "text": "pass", "rev": 5, "hidden": True},
        {"role": "bot", "text": "legacy"},
    ]
    assert unread_for_group(messages, open_attention=2, read_rev=1) == 3
    assert unread_for_group(messages, open_attention=0, read_rev=5) == 0


def test_sync_and_read_endpoints(rev_env, client):
    store = GroupStore(rev_env["root"])
    group = store.create(name="room", bot_ids=["chef"], admin_bot_id="chef")
    sync = client.get("/api/v1/bots/sync", params={"since": 0})
    assert sync.status_code == 200
    assert sync.json() == {"rev": 0, "changed": False}

    store.append_message(group.id, role="user", text="hello")
    changed = client.get("/api/v1/bots/sync", params={"since": 0})
    assert changed.json() == {"rev": 1, "changed": True}
    same = client.get("/api/v1/bots/sync", params={"since": 1})
    assert same.json() == {"rev": 1, "changed": False}

    marked = client.post(f"/api/v1/bots/groups/{group.id}/read", json={"rev": 1})
    assert marked.status_code == 200
    assert marked.json() == {"group_id": group.id, "read_rev": 1}

    inbox = client.get("/api/v1/bots/inbox")
    assert inbox.status_code == 200
    row = [g for g in inbox.json()["groups"] if g["group_id"] == group.id][0]
    assert row["read_rev"] == 1
    assert row["unread"] == 0
    assert client.post(f"/api/v1/bots/groups/{group.id}/read", json={"rev": -1}).status_code == 422
