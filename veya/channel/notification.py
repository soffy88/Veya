"""Notification Projection (Veya Execution Contract Phase 2).

Projects mission/execution progress, JEV owner interrupts, and lifecycle events
outward into Channel sinks.
"""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from veya.remote.events import VeyaEvent

from .store import ChannelStore


class NotificationLevel(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    ERROR = "ERROR"


class NotificationCategory(StrEnum):
    PROGRESS = "PROGRESS"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class Notification:
    notification_id: str
    channel_id: str
    mission_id: str
    title: str
    body: str
    level: NotificationLevel = NotificationLevel.INFO
    category: NotificationCategory = NotificationCategory.PROGRESS
    execution_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    delivered: bool = False
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "channel_id": self.channel_id,
            "mission_id": self.mission_id,
            "title": self.title,
            "body": self.body,
            "level": str(self.level),
            "category": str(self.category),
            "execution_id": self.execution_id,
            "data": dict(self.data),
            "delivered": self.delivered,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Notification:
        return cls(
            notification_id=str(data["notification_id"]),
            channel_id=str(data["channel_id"]),
            mission_id=str(data["mission_id"]),
            title=str(data.get("title", "")),
            body=str(data.get("body", "")),
            level=NotificationLevel(str(data.get("level", "INFO")).upper()),
            category=NotificationCategory(str(data.get("category", "PROGRESS")).upper()),
            execution_id=data.get("execution_id"),
            data=dict(data.get("data") or {}),
            delivered=bool(data.get("delivered", False)),
            created_at=float(data.get("created_at", time.time())),
        )


NotificationSink = Callable[[Notification], Awaitable[None] | None]


class NotificationProjector:
    """Manages outward projection of notifications to Channel logs and sinks."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.channels = ChannelStore(project_root)
        self._sinks: list[NotificationSink] = []

    def add_sink(self, sink: NotificationSink) -> None:
        """Register a delivery sink (e.g. SSE, WebSocket, webhook, IM)."""
        if sink not in self._sinks:
            self._sinks.append(sink)

    def _notification_file(self, channel_id: str) -> Path:
        return self.channels.channel_dir(channel_id) / "notifications.jsonl"

    async def _emit_to_sinks(self, notification: Notification) -> None:
        for sink in list(self._sinks):
            try:
                res = sink(notification)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                continue

    async def project(
        self,
        channel_id: str,
        mission_id: str,
        title: str,
        body: str,
        *,
        level: NotificationLevel | str = NotificationLevel.INFO,
        category: NotificationCategory | str = NotificationCategory.PROGRESS,
        execution_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Notification:
        """Project a structured notification to durable log and active sinks."""
        lvl = (
            level if isinstance(level, NotificationLevel) else NotificationLevel(str(level).upper())
        )
        cat = (
            category
            if isinstance(category, NotificationCategory)
            else NotificationCategory(str(category).upper())
        )
        notif = Notification(
            notification_id=f"notif_{uuid.uuid4().hex[:16]}",
            channel_id=channel_id,
            mission_id=mission_id,
            title=title,
            body=body,
            level=lvl,
            category=cat,
            execution_id=execution_id,
            data=dict(data or {}),
            created_at=time.time(),
        )

        # Durable append
        path = self._notification_file(channel_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(notif.to_dict(), ensure_ascii=False) + "\n")

        # Emit to sinks
        await self._emit_to_sinks(notif)
        return notif

    async def project_interrupt(
        self,
        channel_id: str,
        mission_id: str,
        interrupt_reason: str,
        details: str = "",
        *,
        execution_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Notification:
        """Project a high-priority owner interruption (JEV / approval gate)."""
        return await self.project(
            channel_id=channel_id,
            mission_id=mission_id,
            title=f"Action Required: {interrupt_reason}",
            body=details or f"Owner confirmation required: {interrupt_reason}",
            level=NotificationLevel.ACTION_REQUIRED,
            category=NotificationCategory.CONFIRMATION_REQUIRED,
            execution_id=execution_id,
            data={"interrupt_reason": interrupt_reason, **(data or {})},
        )

    async def project_event(
        self,
        channel_id: str,
        event: VeyaEvent,
    ) -> Notification:
        """Map canonical VeyaEvent to channel notification."""
        level = NotificationLevel.INFO
        cat = NotificationCategory.PROGRESS
        if "fail" in event.event_type or "unavailable" in event.event_type:
            level = NotificationLevel.ERROR
            cat = NotificationCategory.FAILED
        elif "completed" in event.event_type:
            cat = NotificationCategory.COMPLETED

        title = f"Execution Event: {event.event_type}"
        msg = (
            event.payload.get("message")
            or f"Execution {event.execution_id} transition: {event.event_type}"
        )
        return await self.project(
            channel_id=channel_id,
            mission_id=event.mission_id,
            title=title,
            body=str(msg),
            level=level,
            category=cat,
            execution_id=event.execution_id,
            data=event.payload,
        )

    def list_notifications(self, channel_id: str, limit: int = 50) -> list[Notification]:
        path = self._notification_file(channel_id)
        if not path.is_file():
            return []
        items: list[Notification] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(Notification.from_dict(json.loads(line)))
            except Exception:
                continue
        return items[-limit:] if limit else items


__all__ = [
    "Notification",
    "NotificationCategory",
    "NotificationLevel",
    "NotificationProjector",
    "NotificationSink",
]
