"""Queue visibility: /sync carries the running + queued turn snapshot."""

from __future__ import annotations

import pytest

from server.nano_w10.turn_queue import TurnQueue, queue_snapshot


def test_snapshot_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VEYA_TURN_QUEUE", raising=False)
    snapshot = queue_snapshot()
    assert snapshot == {"enabled": False, "running": [], "pending": []}


def test_snapshot_enabled_reports_empty_queue(monkeypatch):
    monkeypatch.setenv("VEYA_TURN_QUEUE", "1")
    snapshot = queue_snapshot()
    assert snapshot["enabled"] is True
    assert snapshot["running"] == []
    assert snapshot["pending"] == []


def test_queue_snapshots_shape():
    queue = TurnQueue()
    assert queue.running_snapshot() == []
    assert queue.pending_snapshot() == []


@pytest.mark.asyncio
async def test_sync_response_carries_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_BOTS_ROOT", str(tmp_path / "bots-root"))
    monkeypatch.delenv("VEYA_TURN_QUEUE", raising=False)
    from server.routes.bot_team import get_sync

    body = await get_sync(since=0, user={"user_id": "q-user", "username": "q"})
    assert body["queue"] == {"enabled": False, "running": [], "pending": []}
    assert set(body) == {"rev", "changed", "queue"}
