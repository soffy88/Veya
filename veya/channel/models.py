"""Channel data models (Veya Execution Contract Phase 2).

A Channel is the durable communication medium and ambient context container
situated above Mission:
  Channel
    └─ Mission*
         └─ Execution*
              └─ Session*
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ChannelType(StrEnum):
    WEB = "web"
    CLI = "cli"
    IM = "im"
    WEBHOOK = "webhook"
    DAEMON = "daemon"


class ChannelStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"


@dataclass
class Channel:
    channel_id: str
    channel_type: ChannelType
    name: str
    workspace_root: str = ""
    status: ChannelStatus = ChannelStatus.ACTIVE
    config: dict[str, Any] = field(default_factory=dict)
    active_mission_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_type": str(self.channel_type),
            "name": self.name,
            "workspace_root": self.workspace_root,
            "status": str(self.status),
            "config": dict(self.config),
            "active_mission_ids": list(self.active_mission_ids),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Channel:
        return cls(
            channel_id=str(data["channel_id"]),
            channel_type=ChannelType(str(data.get("channel_type", "cli")).lower()),
            name=str(data.get("name", "")),
            workspace_root=str(data.get("workspace_root", "")),
            status=ChannelStatus(str(data.get("status", "ACTIVE")).upper()),
            config=dict(data.get("config") or {}),
            active_mission_ids=[str(m) for m in data.get("active_mission_ids") or []],
            metadata=dict(data.get("metadata") or {}),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


__all__ = ["Channel", "ChannelStatus", "ChannelType"]
