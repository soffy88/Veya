"""
layer4/server/sse.py — Server-Sent Events streaming

Converts durable session events → SSE stream for frontend consumption.
Strictly relies on the canonical durable event journal and cursor.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from server.events import _to_envelope
from server.session_events import durable_session_store, is_terminal

router = APIRouter(prefix="/stream", tags=["sse"])

_HEARTBEAT_S = 20.0
_RETRY_MS = 3000


def parse_cursor(cursor_str: str) -> tuple[int, int]:
    parts = cursor_str.split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    raise ValueError("Invalid cursor format")


async def events_generator(session_id: str, request: Request | None) -> AsyncIterator[str]:
    # 0. Check disconnect
    if request is not None and await request.is_disconnected():
        return

    # 1. Parse Last-Event-ID (epoch:seq)
    client_epoch = -1
    client_seq = -1
    lei_header = request.headers.get("Last-Event-ID") if request else None

    current_epoch, current_seq = await durable_session_store.get_stream_head(session_id)
    if current_epoch == 1 and current_seq == 0:
        # Stream doesn't exist yet, that's fine, we start from epoch 1
        current_epoch = 1

    if lei_header:
        try:
            client_epoch, client_seq = parse_cursor(lei_header)
        except ValueError:
            yield 'event: error\ndata: {"error": "CURSOR_STALE"}\n\n'
            return

        if client_epoch != current_epoch:
            # Wrong epoch -> STALE_CURSOR
            yield 'event: error\ndata: {"error": "CURSOR_STALE"}\n\n'
            return

        if client_seq > current_seq:
            # Future seq
            yield 'event: error\ndata: {"error": "CURSOR_AHEAD"}\n\n'
            return

    yield f"retry: {_RETRY_MS}\n\n"

    # Subscribe LIVE first, then CATCH-UP to ensure NO GAP
    sub_q = durable_session_store.subscribe_live(session_id)
    try:
        # CATCH-UP
        catchup_seq = client_seq if client_seq >= 0 else 0
        catchup_events = await durable_session_store.catch_up(
            session_id, current_epoch, catchup_seq
        )

        last_delivered_seq = catchup_seq
        for ev in catchup_events:
            if ev["epoch"] != current_epoch or ev["seq"] <= last_delivered_seq:
                continue
            payload = json.dumps(ev, ensure_ascii=False)
            yield f"id: {ev['id']}\ndata: {payload}\n\n"
            last_delivered_seq = ev["seq"]
            # A durable terminal event ends the stream. This replaces the old
            # in-memory close() sentinel, which had no durable representation:
            # without it a finished stream stayed open until the client gave up.
            if is_terminal(ev):
                yield "data: [DONE]\n\n"
                return

        # LIVE TAIL
        while True:
            try:
                item = await asyncio.wait_for(sub_q.get(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                if request is not None and await request.is_disconnected():
                    return
                yield ": ping\n\n"
                continue

            if item is None:
                yield "data: [DONE]\n\n"
                return

            ev_epoch, ev_seq = item["epoch"], item["seq"]
            # Skip anything from a previous turn on this session id, and anything
            # the catchup already delivered.
            if ev_epoch != current_epoch or ev_seq <= last_delivered_seq:
                continue

            last_delivered_seq = ev_seq
            payload = json.dumps(item, ensure_ascii=False)
            yield f"id: {item['id']}\ndata: {payload}\n\n"
            if is_terminal(item):
                yield "data: [DONE]\n\n"
                return

    finally:
        durable_session_store.unsubscribe_live(session_id, sub_q)


def sse_frame_for_lifecycle(event: Any) -> str:
    """Project a LifecycleEvent to an SSE frame. Pure projection, no I/O."""
    from server.lifecycle_events import project_event_to_sse

    return project_event_to_sse(event)


def emit_lifecycle(
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    bus: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Emit a lifecycle event through the bus and project to session journal.

    This is the production entry point for lifecycle events. It emits through
    the LifecycleEventBus (durable journal) and projects to the session journal
    for SSE consumption.
    """
    if bus is None:
        from server.events import get_lifecycle_bus

        bus = get_lifecycle_bus()
    event = bus.emit(event_type, session_id=session_id, payload=payload, **kwargs)

    async def _project() -> None:
        try:
            from server.session_events import durable_session_store

            await durable_session_store.persist_lifecycle_event(session_id, event)
        except Exception:
            pass

    try:
        import asyncio

        loop = asyncio.get_running_loop()
        _task = loop.create_task(_project())  # noqa: RUF006 — task lifetime is owned by the process
    except RuntimeError:
        pass
    return event


def emit(session_id: str, event: str, data: dict[str, Any]) -> None:
    """Synchronous producer entry point.

    Routes through the durable store's ordered drain rather than spawning an
    untracked task per event, so journal ordering is preserved and append
    failures are recorded and republished as an `error` event instead of being
    silently dropped. Awaitable callers should use
    ``durable_session_store.publish(...)`` directly.
    """
    # Envelope is built from {"event": ..., **data} exactly as the pre-9876111f
    # emit() did, so the wire payload keeps `event` at the top level. session_id
    # is folded in *before* enveloping so trace_id is populated (the old
    # SSEQueue.on_step set it afterwards, leaving trace_id empty).
    payload_in = {"event": event, **data}
    payload_in.setdefault("session_id", session_id)
    envelope = _to_envelope(payload_in)
    envelope.setdefault("session_id", session_id)
    durable_session_store.publish_sync(session_id, envelope, event_type=event)


async def execution_events_generator(
    execution_id: str, request: Request | None, last_seen_sequence: int
) -> AsyncIterator[str]:
    """Replay durable execution events, then attach to the same append stream.

    Subscription is established before replay so events produced during replay are queued;
    the cursor gate drops duplicates and therefore cannot lose the reconnect boundary.
    """
    from veya.remote.execution import ExecutionStore
    from veya.remote.execution_events import ExecutionEventStore, ExecutionEventType

    record = ExecutionStore.from_env(default_persistent=True).get(execution_id)
    if record is None:
        yield 'event: error\ndata: {"error":"EXECUTION_NOT_FOUND"}\n\n'
        return
    if last_seen_sequence < 0:
        yield 'event: error\ndata: {"error":"INVALID_CURSOR"}\n\n'
        return
    store = ExecutionEventStore(record.requested_realpath or record.requested_workspace, execution_id)
    subscriber = store.subscribe_live()
    cursor = last_seen_sequence
    try:
        yield f"retry: {_RETRY_MS}\n\n"
        for event in store.replay_after(cursor):
            if event.sequence <= cursor:
                continue
            cursor = event.sequence
            yield f"id: {cursor}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
            if event.event_type == ExecutionEventType.COMPLETED or (
                event.event_type == ExecutionEventType.EXECUTION_STATE_CHANGED
                and event.payload.get("kind") == "status"
                and event.payload.get("status") in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "TIMED_OUT", "TERMINATED"}
            ):
                yield "data: [DONE]\n\n"
                return
        while True:
            if request is not None and await request.is_disconnected():
                return
            try:
                event = await asyncio.to_thread(subscriber.get, True, _HEARTBEAT_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                yield ": ping\n\n"
                continue
            if event.sequence <= cursor:
                continue
            cursor = event.sequence
            yield f"id: {cursor}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
            if event.event_type == ExecutionEventType.COMPLETED or (
                event.event_type == ExecutionEventType.EXECUTION_STATE_CHANGED
                and event.payload.get("kind") == "status"
                and event.payload.get("status") in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "TIMED_OUT", "TERMINATED"}
            ):
                yield "data: [DONE]\n\n"
                return
    finally:
        store.unsubscribe_live(subscriber)


@router.get("/execution/{execution_id}")
async def stream_execution(
    execution_id: str,
    request: Request,
    last_seen_sequence: int = Query(0, ge=0),
) -> StreamingResponse:
    return StreamingResponse(
        execution_events_generator(execution_id, request, last_seen_sequence),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}")
async def stream_session(session_id: str, request: Request) -> StreamingResponse:
    return StreamingResponse(
        events_generator(session_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
