"""Cross-epoch late-finish isolation.

Deterministic reproduction of the race the durable migration introduced:

  turn N starts (epoch N)
  -> turn N is cancelled
  -> turn N+1 starts (epoch N+1)
  -> turn N's _finish finally publishes its terminal event

Without epoch pinning, that late terminal lands in whatever turn is current at
write time, so it terminates turn N+1 early and the client never sees N+1's
answer. With pinning, the late event is recorded inside epoch N and turn N+1
is unaffected.

No sleeps anywhere: turn N's terminal publish is held at a gate (an
asyncio.Event) until turn N+1 has fully completed, so the interleaving is
forced on every run, not won by timing.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from server.chat_stream import new_agent_stream_events
from server.coordinator_master import (
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
async def test_late_finish_from_cancelled_turn_does_not_truncate_next_turn(monkeypatch):
    sid = f"race-{uuid.uuid4().hex[:8]}"
    release_terminal = asyncio.Event()
    terminal_attempted = asyncio.Event()

    real_terminal = DurableSessionEventStore.publish_terminal

    async def gated_terminal(self, session_id, event, epoch=None):
        # Hold ONLY turn 1's terminal (pinned to epoch 1) until turn 2 has
        # finished. Turn 2's own terminal (epoch 2) must pass through.
        #
        # The gate sits here — before the event reaches the ordered drain —
        # rather than inside append_event, because the drain is a single FIFO:
        # blocking inside it would head-of-line-block turn 2's own publishes.
        if session_id == sid and epoch == 1:
            terminal_attempted.set()
            await release_terminal.wait()
        return await real_terminal(self, session_id, event, epoch=epoch)

    monkeypatch.setattr(DurableSessionEventStore, "publish_terminal", gated_terminal)

    async def _mock_chat_stream(self, text: str, session_id: str, **kwargs: Any):
        from server.events import fire_step

        fire_step({"type": "master_start", "session_id": session_id})
        if "second" in text:
            return {"status": "success", "final_answer": "Answer to turn 2"}
        await asyncio.Event().wait()  # turn 1 never finishes on its own

    # Patch the class, never the instance: an instance attribute would survive
    # monkeypatch teardown as a shadow and break subsequent tests' mocks.
    from server.coordinator_master import MasterCoordinator

    monkeypatch.setattr(MasterCoordinator, "chat_stream", _mock_chat_stream)

    # Turn 1 starts; wait until its first frame is out.
    stream1 = new_agent_stream_events("first", session_id=sid, turn_id="turn_1")
    async for frame in stream1:
        if "master_start" in frame:
            break

    # Cancel turn 1 as a background task: its terminal publish will block at
    # the gate, so awaiting it here would deadlock the test itself.
    cancel_task = asyncio.create_task(cancel_session(sid, turn_id="turn_1"))
    await asyncio.wait_for(terminal_attempted.wait(), timeout=10)

    # Turn 2 starts and must complete fully while turn 1's terminal is held.
    stream2 = new_agent_stream_events("second question", session_id=sid, turn_id="turn_2")
    frames2 = [f async for f in stream2]
    assert any("Answer to turn 2" in f for f in frames2), (
        "turn 2 must deliver its answer while turn 1's terminal is still held"
    )

    # Release turn 1's terminal, then let the cancelled cancel_session finish.
    release_terminal.set()
    await asyncio.wait_for(asyncio.shield(cancel_task), timeout=10)
    for _ in range(200):
        await asyncio.sleep(0.01)
        e1 = await durable_session_store.catch_up(sid, 1, 0)
        if any(is_terminal(e) for e in e1):
            break

    # Turn 2's journal is intact: its own terminal closed it, and turn 1's late
    # event is recorded under epoch 1.
    head_epoch, head_seq = await durable_session_store.get_stream_head(sid)
    assert head_epoch == 2, f"head must stay on turn 2's epoch, got {(head_epoch, head_seq)}"
    turn2 = await durable_session_store.catch_up(sid, 2, 0)
    assert any("Answer to turn 2" in str(e.get("data", {})) for e in turn2)
    assert is_terminal(turn2[-1]), "turn 2 must end with its own terminal"
    turn1 = await durable_session_store.catch_up(sid, 1, 0)
    assert any(is_terminal(e) for e in turn1), "turn 1's late terminal must be in epoch 1"
    assert all(e["epoch"] == 2 for e in turn2)


@pytest.mark.asyncio
async def test_pinned_late_event_never_touches_current_head():
    """Deterministic store-level pinning contract (no concurrency, no timing).

    A late event pinned to a superseded epoch must land in that epoch's history
    with the next seq there, and must leave the current head (epoch, seq_head)
    untouched. This is the exact guarantee the end-to-end race test above
    exercises through the full stack.
    """
    sid = f"pin-{uuid.uuid4().hex[:8]}"
    e1 = await durable_session_store.begin_stream(sid)
    await durable_session_store.append_event(sid, "text_delta", {"d": "t1"})
    e2 = await durable_session_store.begin_stream(sid)
    assert e2 == e1 + 1
    # Late producer for the superseded turn:
    late = await durable_session_store.append_event(
        sid, "master_done", {"status": "cancelled"}, epoch=e1
    )
    assert (late["epoch"], late["seq"]) == (e1, 2)
    head = await durable_session_store.get_stream_head(sid)
    assert head == (e2, 0), f"head moved: {head}"
    # Current-turn write still gets seq 1 of the new epoch:
    cur = await durable_session_store.append_event(sid, "text_delta", {"d": "t2"})
    assert (cur["epoch"], cur["seq"]) == (e2, 1)
    assert await durable_session_store.get_stream_head(sid) == (e2, 1)
    # And a pin to the still-current epoch behaves like a normal write:
    cur2 = await durable_session_store.append_event(sid, "text_delta", {"d": "t2b"}, epoch=e2)
    assert (cur2["epoch"], cur2["seq"]) == (e2, 2)
