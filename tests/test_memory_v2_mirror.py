"""Bot core-memory writes reach the Personal Runtime (MemoryRecord v2)."""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture()
def v2_store(monkeypatch, tmp_path):
    from runtime.personal import runtime as personal_runtime
    from runtime.personal.runtime import PersonalRuntimeStore

    store = PersonalRuntimeStore(sqlite_path=tmp_path / "personal.sqlite3", production=False)
    monkeypatch.setattr(personal_runtime, "_PERSONAL_RUNTIME", store)
    monkeypatch.setenv("VEYA_BOT_MEMORY_V2_MIRROR", "1")
    monkeypatch.setenv("VEYA_BOT_MEMORY_MD", "1")
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots"))
    registry = tmp_path / "bot_registry.json"
    registry.write_text(
        json.dumps(
            {
                "bot_mirror": {
                    "bot_id": "bot_mirror",
                    "owner_id": "user_mirror",
                    "name": "Mirror",
                    "runner_id": "local",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(registry))
    return store


def _active(store, owner="user_mirror"):
    return asyncio.run(store.search_memory("", scope_type="user", scope_id=owner, limit=50))


def test_remember_commits_v2_record_and_forget_forgets_it(v2_store):
    from runtime import bot_scope
    from server.bot_team import memory_layers

    token = bot_scope.bind_current_bot("bot_mirror")
    try:
        out = json.loads(memory_layers.remember("用户喜欢黑咖啡"))
        assert out["ok"] and out["v2"] == "committed", out
        rows = _active(v2_store)
        assert [r["content"] for r in rows] == ["用户喜欢黑咖啡"]
        assert rows[0]["scope_type"] == "user" and rows[0]["scope_id"] == "user_mirror"

        gone = json.loads(memory_layers.forget("我喜欢黑咖啡"))
        assert gone["v2"] == "ok", gone
        assert _active(v2_store) == []
    finally:
        bot_scope.reset_current_bot(token)


def test_review_writer_mirrors_bot_scope_on_running_loop(v2_store):
    from server.bot_team import memory_v2_mirror
    from server.bot_team.memory_layers import LayeredMemoryStore
    from server.bot_team.memory_review_bridge import _make_writer

    async def scenario():
        write = _make_writer(LayeredMemoryStore("bot_mirror"))
        assert write("add", "用户住在杭州 [source: part 1 #x; added: 2026-10-13; by: bot_mirror]")[
            "ok"
        ]
        await asyncio.gather(*list(memory_v2_mirror._BG_TASKS))
        active = await v2_store.search_memory(
            "杭州", scope_type="user", scope_id="user_mirror", limit=10
        )
        return active

    # Review entries are model proposals: a reviewable candidate, not an active fact.
    assert asyncio.run(scenario()) == []
    import sqlite3

    conn = sqlite3.connect(str(v2_store.sqlite_path))
    rows = conn.execute(
        "SELECT proposed_content,status FROM memory_candidates WHERE scope_id='user_mirror'"
    ).fetchall()
    assert rows == [("用户住在杭州", "candidate")]


def test_mirror_off_without_execution_db(monkeypatch):
    from server.bot_team.memory_v2_mirror import mirror_enabled

    monkeypatch.delenv("VEYA_BOT_MEMORY_V2_MIRROR", raising=False)
    monkeypatch.delenv("VEYA_EXECUTION_DATABASE_URL", raising=False)
    assert mirror_enabled() is False
    monkeypatch.setenv("VEYA_EXECUTION_DATABASE_URL", "postgresql://x")
    assert mirror_enabled() is True
    monkeypatch.setenv("VEYA_BOT_MEMORY_V2_MIRROR", "0")
    assert mirror_enabled() is False
