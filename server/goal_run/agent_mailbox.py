"""P1-01 Agent Mailbox — addressable running agents with queued/delivered messages.

Delivery modes: STEER, FOLLOW_UP, NOTIFY.
Messages are idempotent by message_id. Cross-session addressing is supported.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class DeliveryMode(StrEnum):
    STEER = "STEER"
    FOLLOW_UP = "FOLLOW_UP"
    NOTIFY = "NOTIFY"


@dataclass(frozen=True)
class AgentMessage:
    """A message between agents."""

    message_id: str
    sender: str
    recipient: str
    delivery_mode: DeliveryMode
    payload: dict[str, Any] = field(default_factory=dict)
    goal_id: str | None = None
    correlation_id: str | None = None
    created_at: float = field(default_factory=time.time)
    delivered_at: float | None = None
    acknowledged_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["delivery_mode"] = self.delivery_mode.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentMessage:
        data = dict(data)
        if isinstance(data.get("delivery_mode"), str):
            data["delivery_mode"] = DeliveryMode(data["delivery_mode"])
        return cls(**data)


class AgentMailbox:
    """Durable mailbox for agent-to-agent messages.

    Supports cross-session addressing: messages are persisted durably and
    can be delivered across session boundaries.
    """

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = Path.home() / ".veya" / "agent_mailbox.json"
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._pending: dict[str, list[AgentMessage]] = {}

    def _read_all(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_all(self, messages: dict[str, list[dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        delivery_mode: DeliveryMode,
        payload: dict[str, Any] | None = None,
        goal_id: str | None = None,
        correlation_id: str | None = None,
        message_id: str | None = None,
    ) -> AgentMessage:
        """Send a message. Idempotent by message_id."""
        msg = AgentMessage(
            message_id=message_id or str(uuid.uuid4()),
            sender=sender,
            recipient=recipient,
            delivery_mode=delivery_mode,
            payload=dict(payload or {}),
            goal_id=goal_id,
            correlation_id=correlation_id,
        )
        with self._lock:
            all_msgs = self._read_all()
            existing = all_msgs.get(recipient, [])
            if any(m["message_id"] == msg.message_id for m in existing):
                return AgentMessage.from_dict(
                    next(m for m in existing if m["message_id"] == msg.message_id)
                )
            existing.append(msg.to_dict())
            all_msgs[recipient] = existing
            self._write_all(all_msgs)
        return msg

    def poll(
        self,
        recipient: str,
        *,
        mark_delivered: bool = True,
    ) -> list[AgentMessage]:
        """Poll pending messages for a recipient. Optionally mark as delivered."""
        with self._lock:
            all_msgs = self._read_all()
            msgs = all_msgs.get(recipient, [])
            result = [AgentMessage.from_dict(m) for m in msgs if m.get("delivered_at") is None]
            if mark_delivered:
                now = time.time()
                for m in msgs:
                    if m.get("delivered_at") is None:
                        m["delivered_at"] = now
                all_msgs[recipient] = msgs
                self._write_all(all_msgs)
            return result

    def acknowledge(self, recipient: str, message_id: str) -> bool:
        """Acknowledge a message. Returns True if found."""
        with self._lock:
            all_msgs = self._read_all()
            msgs = all_msgs.get(recipient, [])
            for m in msgs:
                if m["message_id"] == message_id:
                    m["acknowledged_at"] = time.time()
                    all_msgs[recipient] = msgs
                    self._write_all(all_msgs)
                    return True
            return False

    def backpressure(self, recipient: str) -> int:
        """Return the number of undelivered messages for a recipient."""
        with self._lock:
            msgs = self._read_all().get(recipient, [])
            return sum(1 for m in msgs if m.get("delivered_at") is None)
