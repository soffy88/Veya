"""P0-A: cancel intent binds the canonical turn epoch.

Invariant: cancel must capture the turn epoch at intent time (when
cancel_session is invoked), never lazily at publish time. If cancel_session is
still working through its preamble when the next turn begins, a lazy head-read
attributes this turn's terminal to the next turn and truncates its stream.

Deterministic forced interleaving (no sleeps, no timing):

  turn 1 starts and completes normally (epoch 1)
  -> gate installed on epoch-1 terminal publishes
  -> cancel_session(turn 1) runs in background, blocks at gate
  -> turn 2 begins (epoch 2) and completes fully
  -> gate released
  -> cancel_session's terminal must land in epoch 1, turn 2 untouched

Without eager capture, cancel_session reads the head after turn 2 began and
its terminal lands in epoch 2, ending turn 2's stream early.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from server.chat_stream import new_agent_stream_events
from server.coordinator_master import (
    MasterCoordinator,
    _active_generations,
    _active_stream_sessions,
    _active_streams,
    _active_turn_ids,
    _cancelled_generations,
    _cancelled_turn_ids,
    _last_stop_meta,
    cancel_session,
)
from server.session_events import DurableSessionEventStore, durable_session_store, is_terminal


@pytest.fixture(autouse=True)
def _clean_state():
    _active_streams.clear()
    _active_stream_sessions.clear()
    _active_generations.clear()
    _active_turn_ids.clear()
    _cancelled_generations.clear()
    _cancelled_turn_ids.clear()
    _last_stop_meta.clear()
    yield
    for t in list(_active_streams.values()):
        if not t.done():
            t.cancel()
    _active_streams.clear()
    _active_stream_sessions.clear()
    _active_generations.clear()
    _active_turn_ids.clear()
    _cancelled_generations.clear()
    _cancelled_turn_ids.clear()
    _last_stop_meta.clear()


@pytest.mark.asyncio
async def test_cancel_binds_epoch_at_intent_not_at_publish(monkeypatch):
    sid = f"cancel-epoch-{uuid.uuid4().hex[:8]}"
    release_terminal = asyncio.Event()
    terminal_attempted = asyncio.Event()

    real_publish = DurableSessionEventStore.publish
    gate_armed = asyncio.Event()

    async def gated_publish(self, session_id, event, event_type=None, epoch=None):
        # Hold cancel_session's FIRST publish (the stop text_delta), which
        # precedes its terminal. This forces cancel_session to still be working
        # while turn 2 runs to completion. With a lazy head-read, it would
        # then pin turn 2's epoch; with eager capture at intent time, it keeps
        # turn 1's epoch regardless of when it resumes.
        payload = event if isinstance(event, dict) else {}
        if (
            session_id == sid
            and gate_armed.is_set()
            and isinstance(payload.get("delta"), str)
            and "已停止" in payload["delta"]
        ):
            terminal_attempted.set()
            await release_terminal.wait()
        return await real_publish(self, session_id, event, event_type=event_type, epoch=epoch)

    monkeypatch.setattr(DurableSessionEventStore, "publish", gated_publish)

    async def _mock_chat_stream(self, text: str, session_id: str, **kwargs: Any):
        # Awaited publish: durable before return, so journal order is exact.
        await durable_session_store.publish(
            session_id, {"type": "master_start", "session_id": session_id}
        )
        if "second" in text:
            return {"status": "success", "final_answer": "Answer to turn 2"}
        return {"status": "success", "final_answer": "Answer to turn 1"}

    monkeypatch.setattr(MasterCoordinator, "chat_stream", _mock_chat_stream)

    # Turn 1 runs to completion normally (epoch 1, own terminal, stream ends).
    stream1 = new_agent_stream_events("first", session_id=sid, turn_id="turn_1")
    frames1 = [f async for f in stream1]
    assert any("Answer to turn 1" in f for f in frames1)

    # Late cancel for turn 1 runs in background and blocks at the gate
    # (on its first publish, the stop text_delta).
    # _active_stream_sessions was discarded when turn 1's stream ended, so
    # re-register it: cancel_session only publishes for live sessions.
    _active_stream_sessions.add(sid)
    gate_armed.set()
    cancel_task = asyncio.create_task(cancel_session(sid, turn_id="turn_1"))
    await asyncio.wait_for(terminal_attempted.wait(), timeout=10)

    # Turn 2 begins (epoch 2) and must complete fully while the cancel is held
    # at its first publish (i.e. before it could read the head lazily).
    stream2 = new_agent_stream_events("second question", session_id=sid, turn_id="turn_2")
    frames2 = [f async for f in stream2]
    assert any("Answer to turn 2" in f for f in frames2), (
        "turn 2 must complete while cancel_session's terminal is still held"
    )

    # Release the cancel. Its terminal must land in epoch 1.
    release_terminal.set()
    await asyncio.wait_for(asyncio.shield(cancel_task), timeout=10)
    for _ in range(200):
        await asyncio.sleep(0.01)
        e1 = await durable_session_store.catch_up(sid, 1, 0)
        if sum(1 for e in e1 if is_terminal(e)) >= 2:
            break

    head_epoch, _ = await durable_session_store.get_stream_head(sid)
    assert head_epoch == 2, f"head must stay on turn 2, got epoch {head_epoch}"
    turn2 = await durable_session_store.catch_up(sid, 2, 0)
    assert is_terminal(turn2[-1]), "turn 2 must end with its own terminal"
    assert all(e["epoch"] == 2 for e in turn2)
    # Cancel's terminal is recorded under epoch 1, never as turn 2.
    turn1_terminals = [e for e in await durable_session_store.catch_up(sid, 1, 0) if is_terminal(e)]
    assert len(turn1_terminals) >= 2, (
        "turn 1's own terminal + the late cancel must both be in epoch 1"
    )
