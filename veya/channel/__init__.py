"""Veya Channel & Agent Inbox Package (Veya Execution Contract Phase 2).

Provides the ambient communication and ingestion layer situated above Mission:
  Channel
    └─ Agent Inbox
         └─ Mission*
              └─ Execution*
                   └─ Session*
    ┌─ Notification Projection
"""

from __future__ import annotations

from .inbox import AgentInbox, InboxMessage, InboxMessageStatus, InboxMessageType
from .models import Channel, ChannelStatus, ChannelType
from .notification import (
    Notification,
    NotificationCategory,
    NotificationLevel,
    NotificationProjector,
    NotificationSink,
)
from .store import ChannelStore

__all__ = [
    "AgentInbox",
    "Channel",
    "ChannelStatus",
    "ChannelStore",
    "ChannelType",
    "InboxMessage",
    "InboxMessageStatus",
    "InboxMessageType",
    "Notification",
    "NotificationCategory",
    "NotificationLevel",
    "NotificationProjector",
    "NotificationSink",
]
