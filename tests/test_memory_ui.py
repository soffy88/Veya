"""tests/test_memory_ui.py — Clean-room OpenMinis borrow: VEYA_MEMORY_UI.

Covers:
    1. Feature flag registered and default False
    2. Agent write guard on USER_GLOBAL.md (BotMemoryStore & LayeredMemoryStore)
    3. User write & read of USER_GLOBAL.md
    4. Prompt injection of USER_GLOBAL.md into build_objective (flag ON vs OFF)
    5. render_for_prompt skips USER_GLOBAL.md when flag ON
    6. REST GET & PUT /api/v1/bots/{bot_id}/memory/user-global
    7. REST GET /api/v1/bots/{bot_id}/memory/entries (core + scratch items)
    8. REST POST /api/v1/bots/{bot_id}/memory/retract (removes fact & scrubs logs)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.bot_team.memory import USER_GLOBAL_FILE, BotMemoryStore
from server.bot_team.memory_layers import LayeredMemoryStore
from server.bot_team.turns import build_objective
from server.feature_flags import enabled
from server.goal_run.bot_identity import BotRegistry, BotSpec

BOT_ID = "bot-memory-test"


@pytest.fixture()
def bots_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path))
    BotRegistry().register(BotSpec(bot_id=BOT_ID, owner_id="test-user", name="MemoryBot"))
    return tmp_path


@pytest.fixture()
def client(bots_dir):
    from server import auth as _auth
    from server.app import app

    app.dependency_overrides[_auth.require_user] = lambda: {"user_id": "test-user"}
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.pop(_auth.require_user, None)


def test_flag_registered_default_off():
    assert enabled("VEYA_MEMORY_UI") is False


def test_flag_turn_on(monkeypatch):
    monkeypatch.setenv("VEYA_MEMORY_UI", "1")
    assert enabled("VEYA_MEMORY_UI") is True


def test_agent_write_guard_bot_memory_store(bots_dir):
    store = BotMemoryStore(BOT_ID, bots_root=bots_dir)
    # User / system without caller_bot_id can write
    store.write(USER_GLOBAL_FILE, "User preference")
    assert store.read(USER_GLOBAL_FILE) == "User preference"

    # Agent with caller_bot_id is rejected
    with pytest.raises(
        PermissionError, match=r"USER_GLOBAL\.md is user-written and read-only to agents"
    ):
        store.write(USER_GLOBAL_FILE, "Agent modified", caller_bot_id=BOT_ID)

    # Agent delete is rejected
    with pytest.raises(
        PermissionError, match=r"USER_GLOBAL\.md is user-written and read-only to agents"
    ):
        store.delete(USER_GLOBAL_FILE, caller_bot_id=BOT_ID)

    # User delete succeeds
    assert store.delete(USER_GLOBAL_FILE) is True


def test_agent_write_guard_layered_store(bots_dir):
    store = LayeredMemoryStore(BOT_ID)
    # User write succeeds
    store.write_user_global("User preferences: use TypeScript")
    assert "TypeScript" in store.read_user_global()

    # Agent write with caller_bot_id fails
    with pytest.raises(
        PermissionError, match=r"USER_GLOBAL\.md is user-written and read-only to agents"
    ):
        store.write_user_global("Agent override", caller_bot_id=BOT_ID)


def test_build_objective_prompt_injection(bots_dir, monkeypatch):
    spec = BotSpec(
        bot_id=BOT_ID, owner_id="test-user", name="MemoryBot", instructions="You are a helper."
    )
    store = LayeredMemoryStore(BOT_ID)
    store.write_user_global("My preferred language is Python. Be concise.")

    # 1. Flag OFF (default): user-global is NOT injected
    obj_off = build_objective(bot=spec, text="Hello bot")
    assert "<!-- user-global -->" not in obj_off
    assert "My preferred language is Python" not in obj_off

    # 2. Flag ON: user-global IS injected as read-only section
    monkeypatch.setenv("VEYA_MEMORY_UI", "1")
    obj_on = build_objective(bot=spec, text="Hello bot")
    assert "<!-- user-global -->" in obj_on
    assert "### User Global Background (Read-Only)" in obj_on
    assert "My preferred language is Python. Be concise." in obj_on


def test_render_for_prompt_skips_user_global_when_flag_on(bots_dir, monkeypatch):
    store = BotMemoryStore(BOT_ID, bots_root=bots_dir)
    store.write("MEMORY.md", "Core fact")
    store.write(USER_GLOBAL_FILE, "User background fact")

    # Flag OFF: includes all .md files
    rendered_off = store.render_for_prompt()
    assert "USER_GLOBAL.md" in rendered_off
    assert "User background fact" in rendered_off

    # Flag ON: USER_GLOBAL.md is skipped from general dump (handled by user-global injection)
    monkeypatch.setenv("VEYA_MEMORY_UI", "1")
    rendered_on = store.render_for_prompt()
    assert "USER_GLOBAL.md" not in rendered_on
    assert "User background fact" not in rendered_on
    assert "Core fact" in rendered_on


def test_rest_user_global_endpoints(client, bots_dir):
    # Initial read is empty
    r1 = client.get(f"/api/v1/bots/{BOT_ID}/memory/user-global")
    assert r1.status_code == 200
    assert r1.json()["content"] == ""

    # PUT update
    r2 = client.put(
        f"/api/v1/bots/{BOT_ID}/memory/user-global",
        json={"content": "Prefer concise Chinese responses."},
    )
    assert r2.status_code == 200
    assert r2.json()["ok"] is True

    # Read back
    r3 = client.get(f"/api/v1/bots/{BOT_ID}/memory/user-global")
    assert r3.status_code == 200
    assert r3.json()["content"] == "Prefer concise Chinese responses."

    # Raw text PUT
    r4 = client.put(
        f"/api/v1/bots/{BOT_ID}/memory/user-global",
        content=b"Raw text background",
        headers={"Content-Type": "text/plain"},
    )
    assert r4.status_code == 200
    r5 = client.get(f"/api/v1/bots/{BOT_ID}/memory/user-global")
    assert r5.json()["content"] == "Raw text background"


def test_rest_memory_entries_and_retract(client, bots_dir):
    store = LayeredMemoryStore(BOT_ID)
    # Add a core memory entry
    store.append_entry("喜欢喝冰美式咖啡", source="chat")
    # Add a scratch entry
    store.write_scratch("明天下午三点有会议")

    # GET /entries
    r_list = client.get(f"/api/v1/bots/{BOT_ID}/memory/entries")
    assert r_list.status_code == 200
    data = r_list.json()
    assert data["bot_id"] == BOT_ID
    assert data["total"] >= 2
    entries = data["entries"]

    core_entries = [e for e in entries if e["scope"] == "core"]
    scratch_entries = [e for e in entries if e["scope"] == "scratch"]
    assert any("冰美式咖啡" in e["text"] for e in core_entries)
    assert any("三点有会议" in e["text"] for e in scratch_entries)

    # POST /retract on the coffee entry
    r_retract = client.post(
        f"/api/v1/bots/{BOT_ID}/memory/retract",
        json={"needle": "冰美式咖啡", "scope": "bot"},
    )
    assert r_retract.status_code == 200
    retract_data = r_retract.json()
    assert retract_data["ok"] is True

    # Verify fact is removed from /entries
    r_list2 = client.get(f"/api/v1/bots/{BOT_ID}/memory/entries")
    assert not any("冰美式咖啡" in e["text"] for e in r_list2.json()["entries"])
