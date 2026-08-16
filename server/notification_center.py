"""server/notification_center.py — global background-task notification broadcast.

Unlike server/sse.py's SSEQueue (one queue per session_id, for a single flow run's
progress), this fans a message out to every connected browser tab — "Genesis
finished while you were looking at Kanban" toasts, plus a HITL_REQUIRED variant
that doesn't auto-dismiss and carries an approve/reject payload.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

NotificationType = Literal["INFO", "SUCCESS", "ERROR", "HITL_REQUIRED"]


class NotificationCenter:
    """In-memory broadcaster: one asyncio.Queue per connected client.

    支持按用户过滤: 客户端连接时声明 user_id, 只收到 ``user_id`` 匹配或
    全局 (user_id 为空) 的消息 — 多用户分离 + 多端同步 (手机发命令,
    电脑端同用户实时收到通知/审批)。
    """

    def __init__(self) -> None:
        self._clients: list[tuple[asyncio.Queue[dict | None], str]] = []
        # 已广播消息注册表: id -> message (供 HITL 审批端点回查 payload, 如 vault task_id)
        self._messages: dict[str, dict[str, Any]] = {}

    def connect(self, user_id: str = "") -> asyncio.Queue[dict | None]:
        q: asyncio.Queue[dict | None] = asyncio.Queue()
        self._clients.append((q, user_id))
        return q

    def disconnect(self, q: asyncio.Queue[dict | None]) -> None:
        self._clients = [(cq, uid) for (cq, uid) in self._clients if cq is not q]

    def push(
        self,
        type: NotificationType,
        title: str,
        content: str,
        payload: dict[str, Any] | None = None,
        user_id: str = "",
    ) -> str:
        """Synchronous — safe to call from anywhere already inside the event loop
        (matches server/events.py's fire_step calling convention). Returns the
        message id so callers can later dismiss or resolve it (e.g. vault HITL
        toasts map task_id -> notification_id). user_id="" → 全局广播。"""
        message = {
            "id": uuid.uuid4().hex,
            "type": type,
            "title": title,
            "content": content,
            "payload": payload or {},
            "user_id": user_id,
        }
        self._messages[message["id"]] = message
        for q, uid in list(self._clients):
            if user_id and uid != user_id:
                continue  # 用户级消息只推给所属用户; 匿名/其他用户只收全局
            q.put_nowait(message)
        return message["id"]

    def get(self, notification_id: str) -> dict[str, Any] | None:
        """回查已广播的消息(含 payload) — HITL 审批端点据此解析任务引用。"""
        return self._messages.get(notification_id)

    def dismiss(self, notification_id: str) -> None:
        """Broadcast a control frame telling every connected tab to drop this toast
        (e.g. after one tab approves/rejects a HITL_REQUIRED notification)."""
        self._messages.pop(notification_id, None)
        frame = {"type": "DISMISS", "payload": {"id": notification_id}}
        for q, _uid in list(self._clients):
            q.put_nowait(frame)

    async def stream(self, user_id: str = "") -> AsyncIterator[str]:
        q = self.connect(user_id)
        try:
            while True:
                message = await q.get()
                if message is None:
                    return
                yield f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
        finally:
            self.disconnect(q)


# module-level singleton (server-wide)
global_notifier = NotificationCenter()
