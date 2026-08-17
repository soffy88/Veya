from __future__ import annotations

import asyncio
import sqlite3

import pytest

from veya.oservi.history_store import SqliteHistoryStore


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
