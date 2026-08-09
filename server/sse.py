"""
layer4/server/sse.py — Server-Sent Events streaming

Converts coordinator on_step callbacks → SSE stream for frontend consumption.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/stream", tags=["sse"])


class SSEQueue:
    """Per-session async queue bridging on_step callbacks → SSE."""

    def __init__(self, session_id: str = "") -> None:
        self.sid = session_id
        self._q: asyncio.Queue[dict | None] = asyncio.Queue()

    def on_step(self, event_dict: dict) -> None:
        """Synchronous callback for engine on_step hooks."""
        self._q.put_nowait(event_dict)

    def close(self) -> None:
        self._q.put_nowait(None)  # sentinel

    async def events(self) -> AsyncIterator[str]:
        while True:
            item = await self._q.get()
            if item is None:
                yield "data: [DONE]\n\n"
                return
            payload = json.dumps(item, ensure_ascii=False)
            yield f"data: {payload}\n\n"


# In-memory registry: session_id → SSEQueue
_queues: dict[str, SSEQueue] = {}


def get_or_create_queue(session_id: str) -> SSEQueue:
    if session_id not in _queues:
        _queues[session_id] = SSEQueue(session_id)
    return _queues[session_id]


def emit(session_id: str, event: str, data: dict[str, Any]) -> None:
    """Emit an SSE event to a session's queue (called from engine on_step)."""
    q = _queues.get(session_id)
    if q:
        q.on_step({"event": event, **data})


@router.get("/{session_id}")
async def stream_session(session_id: str) -> StreamingResponse:
    q = get_or_create_queue(session_id)
    return StreamingResponse(
        q.events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
