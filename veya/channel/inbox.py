"""Agent Inbox (Veya Execution Contract Phase 2).

Provides durable, asynchronous ingestion situated between Channels and Missions.
Enables message buffering, deduplication via idempotency keys, and explicit
Mission attribution.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .store import ChannelStore


class InboxMessageType(StrEnum):
    USER_PROMPT = "user_prompt"
    WEBHOOK_EVENT = "webhook_event"
    INTERRUPT_REPLY = "interrupt_reply"
    SCHEDULE_TRIGGER = "schedule_trigger"


class InboxMessageStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


@dataclass
class InboxMessage:
    message_id: str
    channel_id: str
    message_type: InboxMessageType
    sender: str
    content: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: InboxMessageStatus = InboxMessageStatus.PENDING
    mission_id: str | None = None
    idempotency_key: str | None = None
    created_at: float = field(default_factory=time.time)
    processed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "message_type": str(self.message_type),
            "sender": self.sender,
            "content": self.content,
            "payload": dict(self.payload),
            "status": str(self.status),
            "mission_id": self.mission_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "processed_at": self.processed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InboxMessage:
        return cls(
            message_id=str(data["message_id"]),
            channel_id=str(data["channel_id"]),
            message_type=InboxMessageType(str(data.get("message_type", "user_prompt")).lower()),
            sender=str(data.get("sender", "user")),
            content=str(data.get("content", "")),
            payload=dict(data.get("payload") or {}),
            status=InboxMessageStatus(str(data.get("status", "PENDING")).upper()),
            mission_id=data.get("mission_id"),
            idempotency_key=data.get("idempotency_key"),
            created_at=float(data.get("created_at", time.time())),
            processed_at=float(data["processed_at"])
            if data.get("processed_at") is not None
            else None,
        )


class AgentInbox:
    """Durable Agent Inbox manager."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.channels = ChannelStore(project_root)

    def _inbox_file(self, channel_id: str) -> Path:
        return self.channels.channel_dir(channel_id) / "inbox.jsonl"

    def _load_messages(self, channel_id: str) -> list[InboxMessage]:
        path = self._inbox_file(channel_id)
        if not path.is_file():
            return []
        messages: list[InboxMessage] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(InboxMessage.from_dict(json.loads(line)))
            except Exception:
                continue
        return messages

    def _save_messages(self, channel_id: str, messages: list[InboxMessage]) -> None:
        path = self._inbox_file(channel_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
        temp_path.replace(path)

    def enqueue(
        self,
        channel_id: str,
        content: str,
        *,
        sender: str = "user",
        message_type: InboxMessageType | str = InboxMessageType.USER_PROMPT,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> InboxMessage:
        """Enqueue a new incoming message with idempotency deduplication."""
        messages = self._load_messages(channel_id)
        if idempotency_key:
            for existing in messages:
                if (
                    existing.idempotency_key == idempotency_key
                    and existing.status != InboxMessageStatus.ARCHIVED
                ):
                    return existing

        m_type = (
            message_type
            if isinstance(message_type, InboxMessageType)
            else InboxMessageType(str(message_type).lower())
        )
        msg = InboxMessage(
            message_id=f"msg_{uuid.uuid4().hex[:16]}",
            channel_id=channel_id,
            message_type=m_type,
            sender=sender,
            content=content,
            payload=dict(payload or {}),
            status=InboxMessageStatus.PENDING,
            idempotency_key=idempotency_key,
            created_at=time.time(),
        )
        messages.append(msg)
        self._save_messages(channel_id, messages)
        return msg

    def poll(
        self,
        channel_id: str | None = None,
        *,
        status: InboxMessageStatus | str | None = InboxMessageStatus.PENDING,
        limit: int = 10,
    ) -> list[InboxMessage]:
        """Poll messages from a channel (or all channels)."""
        target_channels = (
            [channel_id] if channel_id else [c.channel_id for c in self.channels.list()]
        )
        status_filter = str(status) if status is not None else None
        results: list[InboxMessage] = []

        for cid in target_channels:
            msgs = self._load_messages(cid)
            for m in msgs:
                if status_filter is None or str(m.status) == status_filter:
                    results.append(m)
                    if len(results) >= limit:
                        return results
        return results

    def get(self, channel_id: str, message_id: str) -> InboxMessage | None:
        for m in self._load_messages(channel_id):
            if m.message_id == message_id:
                return m
        return None

    def bind_mission(self, channel_id: str, message_id: str, mission_id: str) -> InboxMessage:
        """Mark message as dispatched and bind to mission."""
        messages = self._load_messages(channel_id)
        for i, m in enumerate(messages):
            if m.message_id == message_id:
                m.status = InboxMessageStatus.DISPATCHED
                m.mission_id = mission_id
                m.processed_at = time.time()
                messages[i] = m
                self._save_messages(channel_id, messages)
                self.channels.link_mission(channel_id, mission_id)
                return m
        raise KeyError(f"Message {message_id} not found in channel {channel_id}")

    def mark_processed(
        self,
        channel_id: str,
        message_id: str,
        *,
        status: InboxMessageStatus | str = InboxMessageStatus.PROCESSED,
    ) -> InboxMessage:
        messages = self._load_messages(channel_id)
        st = (
            status
            if isinstance(status, InboxMessageStatus)
            else InboxMessageStatus(str(status).upper())
        )
        for i, m in enumerate(messages):
            if m.message_id == message_id:
                m.status = st
                m.processed_at = time.time()
                messages[i] = m
                self._save_messages(channel_id, messages)
                return m
        raise KeyError(f"Message {message_id} not found in channel {channel_id}")


__all__ = ["AgentInbox", "InboxMessage", "InboxMessageStatus", "InboxMessageType"]
