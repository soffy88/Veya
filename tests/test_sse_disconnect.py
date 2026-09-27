"""Durable SSE stream behavior.

Replaces the removed in-memory `SSEQueue` tests. The canonical event authority
is `durable_session_store`; these tests pin the guarantees the old queue used
to provide, expressed against the durable journal:

1. normal termination → a durable terminal event ends the stream;
2. terminal state is durable (survives a new store instance / backend restart);
3. an abandoned stream does not leak live subscribers;
4. disconnect then reconnect resumes by cursor with no gap and no duplicate;
5. events published while disconnected are recovered exactly once.
"""

from __future__ import annotations

import asyncio

import pytest

from server.session_events import (
    DurableSessionEventStore,
    durable_session_store,
    is_terminal,
    wire_payload,
)


@pytest.fixture
async def store():
    await durable_session_store._ensure_started()
    repo = durable_session_store._runtime.repository
    if repo.backend == "sqlite":

        def op(conn):
            conn.execute("DELETE FROM session_streams")
            conn.execute("DELETE FROM session_events")

        await asyncio.to_thread(repo._sqlite_tx, op)
    else:

        async def op_pg(conn):
            await conn.execute("DELETE FROM session_streams")
            await conn.execute("DELETE FROM session_events")

        await repo._pg_tx(op_pg)
    yield durable_session_store


@pytest.mark.asyncio
async def test_normal_termination_emits_durable_terminal_event(store):
    """1. A finished stream is closed by a durable terminal event."""
    sub = store.subscribe_live("s-done")
    await store.publish_terminal("s-done", {"type": "master_done", "status": "success"})

    item = await asyncio.wait_for(sub.get(), timeout=5)
    assert is_terminal(item), "master_done must be recognised as terminal"

    # The terminal state is in the journal, not just in the live queue.
    replayed = await store.catch_up("s-done", item["epoch"], 0)
    assert [e["event"] for e in replayed] == ["master_done"]
    store.unsubscribe_live("s-done", sub)


@pytest.mark.asyncio
async def test_terminal_state_survives_backend_restart(store):
    """2. Termination is durable — a fresh store instance still sees it."""
    await store.publish_terminal("s-gone", {"type": "task_done", "result": "ok"})

    restarted = DurableSessionEventStore()
    events = await restarted.catch_up("s-gone", 1, 0)
    assert len(events) == 1
    assert is_terminal(events[0]), "a reconnecting client learns the stream ended from the journal"


@pytest.mark.asyncio
async def test_abandoned_stream_releases_subscriber(store):
    """3. Unsubscribing releases the live subscriber (no leak)."""
    sub = store.subscribe_live("s-alive")
    assert "s-alive" in store._live_subscribers
    store.unsubscribe_live("s-alive", sub)
    assert "s-alive" not in store._live_subscribers


@pytest.mark.asyncio
async def test_reconnect_after_disconnect_has_no_gap_and_no_duplicate(store):
    """4. Disconnect mid-stream, reconnect by cursor: gapless, no duplicates."""
    # Phase 1: consumer receives 1..3 then disconnects.
    sub1 = store.subscribe_live("s-replay")
    for i in (1, 2, 3):
        await store.publish("s-replay", {"type": "text_delta", "delta": f"d{i}"})
    received = []
    for _ in range(3):
        received.append(await asyncio.wait_for(sub1.get(), timeout=5))
    last_seq = received[-1]["seq"]
    store.unsubscribe_live("s-replay", sub1)  # client disconnects

    # Phase 2: producer keeps going while nobody is listening.
    for i in (4, 5, 6):
        await store.publish("s-replay", {"type": "text_delta", "delta": f"d{i}"})

    # Phase 3: reconnect with Last-Event-ID = epoch:seq.
    sub2 = store.subscribe_live("s-replay")
    epoch, _head = await store.get_stream_head("s-replay")
    missed = await store.catch_up("s-replay", epoch, last_seq)
    assert [m["seq"] for m in missed] == [4, 5, 6], "missed events recovered exactly once"
    assert len({m["event_id"] for m in missed}) == 3, "no duplicate event_ids"
    store.unsubscribe_live("s-replay", sub2)


@pytest.mark.asyncio
async def test_two_consumers_same_session_are_isolated(store):
    """Two consumers on one session keep independent cursors."""
    for i in range(1, 11):
        await store.publish("s-two", {"type": "text_delta", "delta": f"d{i}"})

    a = await store.catch_up("s-two", 1, 3)
    b = await store.catch_up("s-two", 1, 7)
    assert [e["seq"] for e in a] == list(range(4, 11))
    assert [e["seq"] for e in b] == [8, 9, 10]


@pytest.mark.asyncio
async def test_multiple_sessions_do_not_cross_contaminate(store):
    """Session isolation: each session has its own journal and cursor."""
    await store.publish("s-a", {"type": "text_delta", "delta": "a1"})
    await store.publish("s-b", {"type": "text_delta", "delta": "b1"})
    await store.publish("s-a", {"type": "text_delta", "delta": "a2"})

    a_events = await store.catch_up("s-a", 1, 0)
    b_events = await store.catch_up("s-b", 1, 0)
    assert [e["data"]["delta"] for e in a_events] == ["a1", "a2"]
    assert [e["data"]["delta"] for e in b_events] == ["b1"]
    assert all(e["session_id"] == "s-a" for e in a_events)
    assert all(e["session_id"] == "s-b" for e in b_events)


@pytest.mark.asyncio
async def test_wire_payload_preserves_legacy_top_level_shape(store):
    """The pre-9876111f wire shape is preserved: `type` stays top-level."""
    ev = await store.publish("s-shape", {"type": "text_delta", "squad_id": "master", "delta": "x"})
    out = wire_payload(ev)
    assert out["type"] == "text_delta"
    assert out["squad_id"] == "master"
    assert out["delta"] == "x"
    assert out["id"] == ev["id"]
    assert out["seq"] == ev["seq"]


@pytest.mark.asyncio
async def test_new_turn_on_same_session_gets_fresh_epoch(store):
    """A second turn on the same session_id must not inherit turn 1's terminal event.

    The removed SSEQueue was popped from a per-session registry on close, so a new
    turn started clean. The durable journal is per-session and never truncated, so
    turn separation rides on the `epoch` column.
    """
    e1 = await store.begin_stream("s-turn")
    await store.publish("s-turn", {"type": "text_delta", "delta": "turn1"})
    await store.publish_terminal("s-turn", {"type": "master_done", "status": "success"})

    e2 = await store.begin_stream("s-turn")
    assert e2 == e1 + 1, "each new turn advances the epoch"

    # Turn 2 replay sees only its own events, not turn 1's terminal.
    turn2 = await store.catch_up("s-turn", e2, 0)
    assert turn2 == []
    await store.publish("s-turn", {"type": "text_delta", "delta": "turn2"})
    turn2 = await store.catch_up("s-turn", e2, 0)
    assert [e["data"]["delta"] for e in turn2] == ["turn2"]
    assert not any(is_terminal(e) for e in turn2), "turn 1's terminal must not leak"

    # Turn 1's events are still durable and readable under the old epoch.
    turn1 = await store.catch_up("s-turn", e1, 0)
    assert [e["event"] for e in turn1] == ["text_delta", "master_done"]


@pytest.mark.asyncio
async def test_stale_epoch_cursor_detects_previous_turn(store):
    """A cursor from a previous turn is detectable via the epoch mismatch."""
    e1 = await store.begin_stream("s-stale-epoch")
    await store.publish("s-stale-epoch", {"type": "text_delta", "delta": "old"})
    e2 = await store.begin_stream("s-stale-epoch")
    assert e1 != e2


@pytest.mark.asyncio
async def test_publish_terminal_rejects_non_terminal_type(store):
    """Guard: only genuinely terminal types may close a stream."""
    with pytest.raises(ValueError):
        await store.publish_terminal("s-bad", {"type": "text_delta", "delta": "x"})


@pytest.mark.asyncio
async def test_sync_publish_preserves_order_via_ordered_drain(store):
    """Sync producers keep FIFO order through the drain (no fire-and-forget)."""
    for i in range(20):
        store.publish_sync("s-drain", {"type": "text_delta", "delta": f"d{i}"})
    # Let the drain task run to completion.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(await store.catch_up("s-drain", 1, 0)) == 20:
            break
    events = await store.catch_up("s-drain", 1, 0)
    assert [e["seq"] for e in events] == list(range(1, 21))
    assert [e["data"]["delta"] for e in events] == [f"d{i}" for i in range(20)]
    assert store.drain_failures == []


@pytest.mark.asyncio
async def test_stream_route_terminates_on_durable_terminal_event(store):
    """`/stream/{session_id}` must end on a terminal event, not hang.

    The in-memory queue this replaced signalled end-of-stream with a `None`
    sentinel pushed by close(). That sentinel had no durable representation, so
    the durable route had no way to end a finished stream and stayed open until
    the client gave up.
    """
    import json as _json

    from server.sse import events_generator

    sid = "s-route-term"
    await store.publish(sid, {"type": "text_delta", "delta": "x"})
    await store.publish_terminal(sid, {"type": "master_done", "status": "success"})

    frames = [f async for f in events_generator(sid, None)]
    assert frames[-1] == "data: [DONE]\n\n", "stream must end with [DONE]"
    assert not any(": ping" in f for f in frames), "must not idle before terminating"
    # Each event frame is one string: "id: <cursor>\ndata: <json>\n\n".
    bodies = [f.split("\ndata: ", 1)[1] for f in frames if "\ndata: " in f]
    assert len(bodies) == 2, f"expected 2 replayed events, got {len(bodies)}"
    first = _json.loads(bodies[0])
    assert first["id"] == "1:1" and first["seq"] == 1


@pytest.mark.asyncio
async def test_stream_route_does_not_replay_previous_turn(store):
    """A previous turn's events must not leak into the current turn's stream."""
    import json as _json

    from server.sse import events_generator

    sid = "s-route-turn"
    e1 = await store.begin_stream(sid)
    await store.publish(sid, {"type": "text_delta", "delta": "turn1"})
    await store.publish_terminal(sid, {"type": "master_done", "status": "ok"})
    e2 = await store.begin_stream(sid)
    await store.publish(sid, {"type": "text_delta", "delta": "turn2"})
    await store.publish_terminal(sid, {"type": "master_done", "status": "ok"})

    frames = [f async for f in events_generator(sid, None)]
    assert frames[-1] == "data: [DONE]\n\n"
    events = [_json.loads(f.split("\ndata: ", 1)[1]) for f in frames if "\ndata: " in f]
    # Current turn only: turn2's text_delta plus its terminal master_done.
    assert [e["epoch"] for e in events] == [e2, e2], f"leaked another epoch: {events}"
    assert [e["seq"] for e in events] == [1, 2], f"unexpected seqs: {events}"
    assert events[0]["data"].get("delta") == "turn2"
    assert is_terminal(events[1])
    assert not any(e["data"].get("delta") == "turn1" for e in events), "turn1 leaked"
    assert e2 == e1 + 1
