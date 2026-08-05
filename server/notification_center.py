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
    """In-memory broadcaster: one asyncio.Queue per connected client."""

    def __init__(self) -> None:
        self._clients: list[asyncio.Queue[dict | None]] = []

    def connect(self) -> asyncio.Queue[dict | None]:
        q: asyncio.Queue[dict | None] = asyncio.Queue()
        self._clients.append(q)
        return q

    def disconnect(self, q: asyncio.Queue[dict | None]) -> None:
        if q in self._clients:
            self._clients.remove(q)

    def push(
        self,
        type: NotificationType,
        title: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Synchronous — safe to call from anywhere already inside the event loop
        (matches server/events.py's fire_step calling convention)."""
        message = {
            "id": uuid.uuid4().hex,
            "type": type,
            "title": title,
            "content": content,
            "payload": payload or {},
        }
        for q in list(self._clients):
            q.put_nowait(message)

    def dismiss(self, notification_id: str) -> None:
        """Broadcast a control frame telling every connected tab to drop this toast
        (e.g. after one tab approves/rejects a HITL_REQUIRED notification)."""
        frame = {"type": "DISMISS", "payload": {"id": notification_id}}
        for q in list(self._clients):
            q.put_nowait(frame)

    async def stream(self) -> AsyncIterator[str]:
        q = self.connect()
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
