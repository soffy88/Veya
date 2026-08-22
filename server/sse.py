"""
layer4/server/sse.py — Server-Sent Events streaming

Converts coordinator on_step callbacks → SSE stream for frontend consumption.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from server.events import _to_envelope

router = APIRouter(prefix="/stream", tags=["sse"])

# 队列静默 >20s 发 SSE 注释心跳 (: ping) — 保持响应体流动 (防 Cloudflare 100s
# 无数据掐断), 同时给「客户端断开」检测一个固定轮询节拍。
_HEARTBEAT_S = 20.0


class SSEQueue:
    """Per-session async queue bridging on_step callbacks → SSE."""

    def __init__(self, session_id: str = "") -> None:
        self.sid = session_id
        self._q: asyncio.Queue[dict | None] = asyncio.Queue()

    def on_step(self, event_dict: dict) -> None:
        """Synchronous callback for engine on_step hooks."""
        self._q.put_nowait(_to_envelope(event_dict))

    def close(self) -> None:
        self._q.put_nowait(None)  # sentinel

    async def events(self, request: Request | None = None) -> AsyncIterator[str]:
        """消费队列 → SSE 帧。

        断开检测: 无 ``None`` 哨兵时 (生产者仍在运行/永不收尾), 裸
        ``await self._q.get()`` 会永久悬挂且 ``_queues`` 泄漏。改为带心跳超时的
        等待, 每个静默节拍检查 ``request.is_disconnected()`` — 客户端已走则退出
        生成器, ``finally`` 清理会话队列, 释放协程与内存。
        """
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._q.get(), timeout=_HEARTBEAT_S)
                except TimeoutError:
                    if request is not None and await request.is_disconnected():
                        return
                    yield ": ping\n\n"  # 保活注释行, EventSource 规范忽略
                    continue
                if item is None:
                    yield "data: [DONE]\n\n"
                    return
                payload = json.dumps(item, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        finally:
            # 消费结束/断开/取消 → 清理会话队列, 防 _queues 无限增长
            _queues.pop(self.sid, None)


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
async def stream_session(session_id: str, request: Request) -> StreamingResponse:
    q = get_or_create_queue(session_id)
    return StreamingResponse(
        q.events(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
