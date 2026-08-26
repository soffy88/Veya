"""
layer4/server/sse.py — Server-Sent Events streaming

Converts coordinator on_step callbacks → SSE stream for frontend consumption.

Reliability features (P3-03):
- Last-Event-ID resumption with bounded replay buffer
- Heartbeat + explicit retry guidance
- Backpressure protection with bounded queue
- Event IDs for resumption
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from server.events import _to_envelope, current_task_id, event_store

router = APIRouter(prefix="/stream", tags=["sse"])

# 队列静默 >20s 发 SSE 注释心跳 (: ping) — 保持响应体流动 (防 Cloudflare 100s
# 无数据掐断), 同时给「客户端断开」检测一个固定轮询节拍。
_HEARTBEAT_S = 20.0
# 重连重试指引 (ms)
_RETRY_MS = 3000
# 队列上限：防止慢客户端导致内存无界增长
_QUEUE_MAXSIZE = 500
# 重放缓冲区大小：保留最近 N 条事件供 Last-Event-ID 续传
_REPLAY_BUFFER_SIZE = 100
# put 超时：背压下不无限阻塞生产者
_PUT_TIMEOUT_S = 0.5


class SSEQueue:
    """Per-session async queue bridging on_step callbacks → SSE.

    Reliability features:
    - Monotonically increasing event_id for Last-Event-ID resumption
    - Bounded replay buffer (deque) for missed-event replay
    - Bounded asyncio.Queue with put timeout for backpressure
    - Heartbeat + retry guidance
    """

    def __init__(self, session_id: str = "") -> None:
        self.sid = session_id
        self._q: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._event_id = 0
        # 环形缓冲区：存 (event_id, envelope) 供重连补发
        self._replay_buffer: deque[tuple[int, dict]] = deque(maxlen=_REPLAY_BUFFER_SIZE)

    def on_step(self, event_dict: dict) -> None:
        """Synchronous callback for engine on_step hooks.

        Assigns event_id, stores in replay buffer, enqueues with backpressure.
        Non-blocking: if queue full, drops oldest replayed event and emits
        a backpressure signal (consumer will see it on next iteration).
        """
        envelope = _to_envelope(event_dict)
        if not envelope.get("session_id"):
            envelope["session_id"] = self.sid
        task_id = current_task_id()
        if task_id and not envelope.get("task_id"):
            envelope["task_id"] = task_id
        with contextlib.suppress(Exception):
            event_store.append(envelope)
        self._event_id += 1
        envelope["id"] = self._event_id
        self._replay_buffer.append((self._event_id, envelope))

        try:
            self._q.put_nowait(envelope)
        except asyncio.QueueFull:
            # 背压：队列满，发信号给消费端（下一轮会读到）
            # 同时丢弃最旧的 replay 条目（已无法补发）
            backpressure_env = {
                "event": "backpressure",
                "id": self._event_id,
                "data": {"reason": "queue_full", "dropped": 1},
            }
            self._replay_buffer.append((self._event_id, backpressure_env))
            try:
                self._q.put_nowait(backpressure_env)
            except asyncio.QueueFull:
                # 极端情况下连背压事件都塞不进去，静默丢弃最旧项
                try:
                    self._q.get_nowait()
                    self._q.put_nowait(backpressure_env)
                except asyncio.QueueEmpty:
                    pass

    def close(self) -> None:
        self._q.put_nowait(None)  # sentinel

    def _replay_from(self, last_event_id: int) -> list[dict]:
        """Return events with id > last_event_id from replay buffer."""
        return [env for eid, env in self._replay_buffer if eid > last_event_id]

    async def events(self, request: Request | None = None) -> AsyncIterator[str]:
        """消费队列 → SSE 帧。

        - 首先检查客户端是否已断开：若已断开，立即返回（不发 retry/心跳）。
        - 首帧发送 retry 指引（若未断开）
        - 支持 Last-Event-ID 头：补发遗漏事件
        - 心跳 + 断开检测
        - 背压事件透传给前端
        - finally 清理会话队列
        """
        # 0. 若客户端在连接时即已断开，直接返回（并清理队列）
        if request is not None and await request.is_disconnected():
            _queues.pop(self.sid, None)
            return

        # 1. 读取 Last-Event-ID 头（标准 SSE 重连机制）
        last_event_id: int | None = None
        if request is not None:
            lei_header = request.headers.get("Last-Event-ID")
            if lei_header and lei_header.isdigit():
                last_event_id = int(lei_header)

        # 2. 发送 retry 指引 + 补发遗漏事件
        yield f"retry: {_RETRY_MS}\n\n"
        replay_highwater = last_event_id if last_event_id is not None else -1
        if last_event_id is not None:
            for env in self._replay_from(last_event_id):
                replay_highwater = max(replay_highwater, int(env.get("id", 0)))
                payload = json.dumps(env, ensure_ascii=False)
                yield f"id: {env['id']}\ndata: {payload}\n\n"

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
                # 正常事件：带 id 字段
                event_id = item.get("id", 0)
                if last_event_id is not None and event_id <= replay_highwater:
                    continue
                payload = json.dumps(item, ensure_ascii=False)
                yield f"id: {event_id}\ndata: {payload}\n\n"
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
