from __future__ import annotations

import asyncio
import sqlite3

import pytest

from veya.oservi.history_store import SqliteHistoryStore


def test_save_is_append_only_old_revisions_survive(tmp_path):
    """docs/dev/rfc-11-state-authority-scoping.md 后续: save() 不再 DELETE+INSERT
    覆盖, 是追加不可变修订。load() 的外部行为(读"当前"消息列表)不变, 但底层
    旧修订必须还能通过 replay() 读到——这是"原始事实不可变"这条原则的直接验证,
    不是读代码猜的。"""
    store = SqliteHistoryStore(tmp_path / "history.db")
    v1 = [{"role": "user", "content": "first"}]
    v2 = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"}]
    v3 = [{"role": "user", "content": "compacted summary only"}]  # 模拟压缩后变短

    store.save_sync("s1", v1, user_id="u")
    store.save_sync("s1", v2, user_id="u")
    store.save_sync("s1", v3, user_id="u")

    # 外部投影: 只看得到最新一条
    assert store.load_sync("s1", user_id="u") == v3

    # 不可变历史: 三条修订全部都在, 按顺序, 没有被后面的 save 覆盖或删除
    history = store.replay_sync("s1", user_id="u")
    assert [h["messages"] for h in history] == [v1, v2, v3]
    assert [h["revision"] for h in history] == [0, 1, 2]


def test_replay_respects_limit_and_session_isolation(tmp_path):
    store = SqliteHistoryStore(tmp_path / "history.db")
    store.save_sync("s1", [{"role": "user", "content": "a"}], user_id="u")
    store.save_sync("s1", [{"role": "user", "content": "b"}], user_id="u")
    store.save_sync("s2", [{"role": "user", "content": "other session"}], user_id="u")

    limited = store.replay_sync("s1", user_id="u", limit=1)
    assert len(limited) == 1
    assert limited[0]["messages"] == [{"role": "user", "content": "a"}]

    full = store.replay_sync("s1", user_id="u")
    assert len(full) == 2


@pytest.mark.asyncio
async def test_async_load_is_not_blocked_by_active_wal_writer(tmp_path) -> None:
    """A WAL reader must not wait for an unrelated uncommitted writer."""
    db_path = tmp_path / "history.db"
    store = SqliteHistoryStore(db_path)
    expected = [{"role": "user", "content": "committed"}]
    store.save_sync("session", expected, user_id="reader")

    writer = sqlite3.connect(db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE turns SET ts=ts WHERE user_id=? AND sid=?",
            ("reader", "session"),
        )

        loaded = await asyncio.wait_for(store.load("session", user_id="reader"), timeout=1)
    finally:
        writer.rollback()
        writer.close()

    assert loaded == expected


@pytest.mark.asyncio
async def test_async_load_and_save_complete_concurrently_within_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_to_thread(*args, **kwargs):
        raise AssertionError("history store must not depend on asyncio's default executor")

    monkeypatch.setattr(asyncio, "to_thread", broken_to_thread)
    store = SqliteHistoryStore(tmp_path / "history.db")
    session_count = 16

    async def save_then_load(index: int) -> list[dict[str, object]]:
        sid = f"session-{index}"
        messages: list[dict[str, object]] = [
            {"role": "user", "content": f"message-{index}", "index": index}
        ]
        await store.save(sid, messages, user_id="concurrent-user")
        return await store.load(sid, user_id="concurrent-user")

    results = await asyncio.wait_for(
        asyncio.gather(*(save_then_load(index) for index in range(session_count))),
        timeout=3,
    )

    assert results == [
        [{"role": "user", "content": f"message-{index}", "index": index}]
        for index in range(session_count)
    ]
