"""Session Continuity and Checkpoint Management for Veya Agent Runtime V1.

Ensures session persistence across daemon restarts, disconnects, and handoffs:
- Checkpoints session state, generation, and last seen event sequence
- Enables replay-safe channel reconnects
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from veya.remote.events import VeyaEvent


@dataclass
class SessionCheckpoint:
    session_id: str
    generation: int
    last_event_seq: int
    last_trigger_id: str | None = None
    checkpoint_reason: str = "periodic"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionCheckpoint:
        return cls(**data)


class SessionContinuityManager:
    """Manages session checkpoints and resume event sequences."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.checkpoints_dir = self.project_root / ".veya" / "runtime" / "checkpoints"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    def _checkpoint_file(self, session_id: str) -> Path:
        return self.checkpoints_dir / f"{session_id}.json"

    def save_checkpoint(
        self,
        session_id: str,
        generation: int,
        last_event_seq: int,
        *,
        last_trigger_id: str | None = None,
        reason: str = "step_completed",
        metadata: dict[str, Any] | None = None,
    ) -> SessionCheckpoint:
        cp = SessionCheckpoint(
            session_id=session_id,
            generation=generation,
            last_event_seq=last_event_seq,
            last_trigger_id=last_trigger_id,
            checkpoint_reason=reason,
            metadata=dict(metadata or {}),
            created_at=time.time(),
        )
        path = self._checkpoint_file(session_id)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(cp.to_dict(), f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        return cp

    def load_checkpoint(self, session_id: str) -> SessionCheckpoint | None:
        path = self._checkpoint_file(session_id)
        if not path.is_file():
            return None
        try:
            return SessionCheckpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def filter_events_for_reconnect(
        self,
        events: list[VeyaEvent],
        last_client_seq: int,
    ) -> list[VeyaEvent]:
        """Filter events to send only those with seq > last_client_seq."""
        return [e for e in events if e.seq > last_client_seq]
