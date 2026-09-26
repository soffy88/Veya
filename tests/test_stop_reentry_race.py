"""Regression tests for stop reentry and reconnect race conditions.

Guarantees:
1. test_stop_cancels_active_chat: Active chat task is cleanly cancelled.
2. test_stop_is_idempotent: Consecutive stop requests return already_stopped safely.
3. test_stale_reconnect_after_stop_does_not_restart: Stale reconnect / retry does not spawn a new task.
4. test_concurrent_stop_and_reconnect_does_not_restart: Concurrent stop & reconnect race is safe.
5. test_new_explicit_turn_after_stop_can_start: A new turn on the same session can run.
6. test_active_streams_cleanup_after_stop: Active stream registry is cleared after stop.
7. test_no_second_master_start_after_stop: No rogue master_start is emitted after stop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server import coordinator_master
from server.chat_stream import new_agent_stream_events
from server.coordinator_master import (
    _active_generations,
    _active_stream_queues,
    _active_streams,
    _active_turn_ids,
    _cancelled_generations,
    _cancelled_turn_ids,
    _last_stop_meta,
    cancel_session,
)
from server.routes.legacy_agent import legacy_agent_stream_status


@pytest.fixture(autouse=True)
def _cleanup_stream_state():
    """Ensure clean coordinator stream state before and after each test."""
    _active_streams.clear()
    _active_generations.clear()
    _active_turn_ids.clear()
    _cancelled_generations.clear()
    _cancelled_turn_ids.clear()
    _last_stop_meta.clear()
    _active_stream_queues.clear()
    yield
    for t in list(_active_streams.values()):
        if not t.done():
            t.cancel()
    _active_streams.clear()
    _active_generations.clear()
    _active_turn_ids.clear()
    _cancelled_generations.clear()
    _cancelled_turn_ids.clear()
    _last_stop_meta.clear()
    _active_stream_queues.clear()


@pytest.mark.asyncio
async def test_stop_cancels_active_chat(monkeypatch):
    """1. test_stop_cancels_active_chat: Cancelling an active chat task works cleanly."""
    sid = "test_sid_stop_1"
    started = asyncio.Event()

    async def _mock_chat_stream(text: str, session_id: str, **kwargs: Any):
        from server.events import emit_step

        emit_step({"type": "master_start", "session_id": session_id, "task_id": "t1"})
        started.set()
        while True:
            await asyncio.sleep(0.05)

    monkeypatch.setattr(coordinator_master.master_coordinator, "chat_stream", _mock_chat_stream)

    stream_iter = new_agent_stream_events("hello world", session_id=sid, turn_id="turn_1")

    # Start generator and pull first frame
    frame = await stream_iter.__anext__()
    assert "master_start" in frame
    await started.wait()

    assert sid in _active_streams
    task = _active_streams[sid]
    assert not task.done()

    # Cancel session
    res = await cancel_session(sid, turn_id="turn_1")
    assert res["status"] == "stopped"
    assert "chat_stream" in res["cancelled"]

    # Verify task cancelled
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    """2. test_stop_is_idempotent: Repeated stop calls do not crash or create tasks."""
    sid = "test_sid_stop_idempotent"

    # Stop when nothing is running
    res1 = await cancel_session(sid, turn_id="turn_none")
    assert res1["status"] == "already_stopped"
    assert res1["cancelled"] == ["none"]

    res2 = await cancel_session(sid, turn_id="turn_none")
    assert res2["status"] == "already_stopped"
    assert res2["cancelled"] == ["none"]


@pytest.mark.asyncio
async def test_stale_reconnect_after_stop_does_not_restart(monkeypatch):
    """3. test_stale_reconnect_after_stop_does_not_restart: Stale retry/reconnect blocked."""
    sid = "test_sid_stale"
    chat_calls = 0

    async def _mock_chat_stream(text: str, session_id: str, **kwargs: Any):
        nonlocal chat_calls
        chat_calls += 1
        from server.events import emit_step

        emit_step({"type": "master_start", "session_id": session_id, "task_id": f"t_{chat_calls}"})
        while True:
            await asyncio.sleep(0.05)

    monkeypatch.setattr(coordinator_master.master_coordinator, "chat_stream", _mock_chat_stream)

    stream_iter1 = new_agent_stream_events("run something", session_id=sid, turn_id="turn_stale_1")
    await stream_iter1.__anext__()
    assert chat_calls == 1

    # Stop this turn
    await cancel_session(sid, turn_id="turn_stale_1")
    assert sid not in _active_streams

    # Stale request with same turn_id arrives
    stale_stream = new_agent_stream_events("run something", session_id=sid, turn_id="turn_stale_1")
    frames = []
    async for frame in stale_stream:
        frames.append(frame)

    # Chat stream was NOT invoked again
    assert chat_calls == 1
    assert any("cancelled" in f for f in frames)
    assert any("[DONE]" in f for f in frames)
    assert sid not in _active_streams


@pytest.mark.asyncio
async def test_concurrent_stop_and_reconnect_does_not_restart(monkeypatch):
    """4. test_concurrent_stop_and_reconnect_does_not_restart: Concurrent stop & reconnect race is safe."""
    sid = "test_sid_concurrent"
    started = asyncio.Event()
    chat_calls = 0

    async def _mock_chat_stream(text: str, session_id: str, **kwargs: Any):
        nonlocal chat_calls
        chat_calls += 1
        from server.events import emit_step

        emit_step({"type": "master_start", "session_id": session_id, "task_id": f"t_{chat_calls}"})
        started.set()
        while True:
            await asyncio.sleep(0.05)

    monkeypatch.setattr(coordinator_master.master_coordinator, "chat_stream", _mock_chat_stream)

    stream_iter = new_agent_stream_events("do work", session_id=sid, turn_id="turn_c1")
    await stream_iter.__anext__()
    await started.wait()
    assert chat_calls == 1

    # Concurrently fire cancel_session and a reconnect stream
    async def _stop():
        return await cancel_session(sid, turn_id="turn_c1")

    async def _reconnect():
        reconnect_iter = new_agent_stream_events("do work", session_id=sid, turn_id="turn_c1")
        frames = []
        try:
            async for f in reconnect_iter:
                frames.append(f)
        except Exception:
            pass
        return frames

    stop_res, _reconnect_frames = await asyncio.gather(_stop(), _reconnect())
    assert stop_res["status"] in ("stopped", "already_stopped")
    assert chat_calls == 1
    assert sid not in _active_streams


@pytest.mark.asyncio
async def test_new_explicit_turn_after_stop_can_start(monkeypatch):
    """5. test_new_explicit_turn_after_stop_can_start: A new explicit turn after stop works normally."""
    sid = "test_sid_new_turn"
    chat_calls = 0

    async def _mock_chat_stream(text: str, session_id: str, **kwargs: Any):
        nonlocal chat_calls
        chat_calls += 1
        from server.events import emit_step

        emit_step({"type": "master_start", "session_id": session_id, "task_id": f"t_{chat_calls}"})
        if chat_calls == 1:
            while True:
                await asyncio.sleep(0.05)
        else:
            emit_step({"type": "text_delta", "delta": "Answer to turn 2"})
            return {"final_answer": "Answer to turn 2"}

    monkeypatch.setattr(coordinator_master.master_coordinator, "chat_stream", _mock_chat_stream)

    # Turn 1 starts then gets stopped
    stream1 = new_agent_stream_events("first question", session_id=sid, turn_id="turn_1")
    await stream1.__anext__()
    assert chat_calls == 1

    await cancel_session(sid, turn_id="turn_1")
    assert sid not in _active_streams

    # Turn 2 starts with new turn_id
    stream2 = new_agent_stream_events("second question", session_id=sid, turn_id="turn_2")
    frames = []
    async for f in stream2:
        frames.append(f)

    assert chat_calls == 2
    assert any("Answer to turn 2" in f for f in frames)


@pytest.mark.asyncio
async def test_active_streams_cleanup_after_stop(monkeypatch):
    """6. test_active_streams_cleanup_after_stop: Active streams map and stream_status are cleaned up."""
    sid = "test_sid_cleanup"
    started = asyncio.Event()

    async def _mock_chat_stream(text: str, session_id: str, **kwargs: Any):
        from server.events import emit_step

        emit_step({"type": "master_start", "session_id": session_id, "task_id": "t1"})
        started.set()
        while True:
            await asyncio.sleep(0.05)

    monkeypatch.setattr(coordinator_master.master_coordinator, "chat_stream", _mock_chat_stream)

    stream = new_agent_stream_events("question", session_id=sid, turn_id="turn_cleanup")
    await stream.__anext__()
    await started.wait()

    status_before = await legacy_agent_stream_status(sid)
    assert status_before == {"active": True}

    await cancel_session(sid, turn_id="turn_cleanup")
    # Brief yield for callbacks to complete
    await asyncio.sleep(0.02)

    status_after = await legacy_agent_stream_status(sid)
    assert status_after == {"active": False}
    assert sid not in _active_streams


@pytest.mark.asyncio
async def test_no_second_master_start_after_stop(monkeypatch):
    """7. test_no_second_master_start_after_stop: No rogue master_start emitted after stop."""
    sid = "test_sid_no_second_start"
    master_starts: list[dict[str, Any]] = []

    async def _mock_chat_stream(text: str, session_id: str, **kwargs: Any):
        from server.events import emit_step

        start_event = {
            "type": "master_start",
            "session_id": session_id,
            "task_id": f"t_{len(master_starts) + 1}",
        }
        master_starts.append(start_event)
        emit_step(start_event)
        while True:
            await asyncio.sleep(0.05)

    monkeypatch.setattr(coordinator_master.master_coordinator, "chat_stream", _mock_chat_stream)

    # Initial turn
    stream = new_agent_stream_events("prompt", session_id=sid, turn_id="turn_orig")
    await stream.__anext__()
    assert len(master_starts) == 1

    # Stop turn
    await cancel_session(sid, turn_id="turn_orig")

    # Stale retries / reconnects
    for _ in range(3):
        stale_stream = new_agent_stream_events("prompt", session_id=sid, turn_id="turn_orig")
        async for _ in stale_stream:
            pass

    # No second master start occurred
    assert len(master_starts) == 1
