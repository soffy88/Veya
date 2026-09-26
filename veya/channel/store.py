"""Durable storage for Channels.

Persists channel metadata using crash-resilient atomic file writes under
`.veya/channels/<channel_id>/channel.json`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Channel, ChannelStatus

_CHANNELS_DIR = ".veya/channels"
_CHANNEL_JSON = "channel.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class ChannelStore:
    """Durable store for Channel records."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)

    def channel_dir(self, channel_id: str) -> Path:
        return self.project_root / _CHANNELS_DIR / channel_id

    def save(self, channel: Channel) -> Channel:
        channel.touch()
        cdir = self.channel_dir(channel.channel_id)
        cdir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(cdir / _CHANNEL_JSON, channel.to_dict())
        return channel

    def get(self, channel_id: str) -> Channel | None:
        path = self.channel_dir(channel_id) / _CHANNEL_JSON
        if not path.is_file():
            return None
        try:
            return Channel.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def list(self, status: ChannelStatus | str | None = None) -> list[Channel]:
        root = self.project_root / _CHANNELS_DIR
        if not root.is_dir():
            return []
        channels: list[Channel] = []
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / _CHANNEL_JSON).is_file():
                ch = self.get(entry.name)
                if ch is None:
                    continue
                if status is not None and str(ch.status) != str(status):
                    continue
                channels.append(ch)
        return channels

    def archive(self, channel_id: str) -> Channel:
        ch = self.get(channel_id)
        if ch is None:
            raise KeyError(f"Channel not found: {channel_id}")
        ch.status = ChannelStatus.ARCHIVED
        return self.save(ch)

    def link_mission(self, channel_id: str, mission_id: str) -> Channel:
        ch = self.get(channel_id)
        if ch is None:
            raise KeyError(f"Channel not found: {channel_id}")
        if mission_id not in ch.active_mission_ids:
            ch.active_mission_ids.append(mission_id)
            return self.save(ch)
        return ch

    def unlink_mission(self, channel_id: str, mission_id: str) -> Channel:
        ch = self.get(channel_id)
        if ch is None:
            raise KeyError(f"Channel not found: {channel_id}")
        if mission_id in ch.active_mission_ids:
            ch.active_mission_ids.remove(mission_id)
            return self.save(ch)
        return ch


__all__ = ["ChannelStore"]
