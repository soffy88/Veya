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

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from server.events import _to_envelope
from server.session_events import durable_session_store

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
            payload = json.dumps(ev, ensure_ascii=False)
            yield f"id: {ev['id']}\ndata: {payload}\n\n"
            last_delivered_seq = ev["seq"]

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
            # Deduplicate items that were already in the catchup
            if ev_epoch == current_epoch and ev_seq <= last_delivered_seq:
                continue

            last_delivered_seq = ev_seq
            payload = json.dumps(item, ensure_ascii=False)
            yield f"id: {item['id']}\ndata: {payload}\n\n"

    finally:
        durable_session_store.unsubscribe_live(session_id, sub_q)


_emit_tasks: set[asyncio.Task] = set()


def emit(session_id: str, event: str, data: dict[str, Any]) -> None:
    """Legacy sync emit. Should now use async append_event.
    This creates an async task to append."""
    envelope = _to_envelope(data)
    envelope.setdefault("session_id", session_id)
    task = asyncio.create_task(durable_session_store.append_event(session_id, event, envelope))
    _emit_tasks.add(task)
    task.add_done_callback(_emit_tasks.discard)


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
