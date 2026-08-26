"""Cancellation safety: a cancelled Master turn is never projected completed."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_master_cancel_writes_cancelled_task_and_checkpoint(tmp_path, monkeypatch):
    from server.coordinator_master import master_coordinator
    from server.events import EventStore, event_store
    from server.task_store import task_store
    from veya.oservi.history_store import SqliteHistoryStore

    event_path = tmp_path / "events.jsonl"
    event_store.path = event_path
    event_store._known_event_ids = None
    task_store.path = tmp_path / "tasks.json"
    task_store.event_store = EventStore(event_path)
    task_store._tasks = {}
    master_coordinator._history_store = SqliteHistoryStore(tmp_path / "history.db")
    master_coordinator._memory_enabled = False
    master_coordinator._session_tree_mirror_enabled = False
    monkeypatch.setenv("VEYA_GRAFT_CONTEXT", "0")

    async def blocked(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(master_coordinator._agent, "chat_stream", blocked)
    running = asyncio.create_task(
        master_coordinator.chat_stream("cancel this", session_id="sess_cancel")
    )
    for _ in range(50):
        if task_store.list(session_id="sess_cancel"):
            break
        await asyncio.sleep(0.01)
    assert task_store.list(session_id="sess_cancel")

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    task = task_store.list(session_id="sess_cancel")[0]
    assert task.status == "cancelled"
    assert task.latest_checkpoint_id
    assert "task.cancelled" in [event["topic"] for event in task_store.events(task.id)]
    assert "checkpoint.created" in [event["topic"] for event in task_store.events(task.id)]
