"""Unified VeyaEvent Lifecycle Fabric (Veya Execution Contract V1 P1).

Provides normalized, typed, causally-chained events for all execution and
runtime state transitions.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class VeyaEventType(StrEnum):
    EXECUTION_CREATED = "execution.created"
    EXECUTION_DISPATCHED = "execution.dispatched"
    EXECUTION_STARTED = "execution.started"
    EXECUTION_SUSPENDING = "execution.suspending"
    EXECUTION_SUSPENDED = "execution.suspended"
    EXECUTION_RESUMING = "execution.resuming"
    EXECUTION_RECOVERING = "execution.recovering"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"
    EXECUTION_CANCELLED = "execution.cancelled"
    EXECUTION_TERMINATED = "execution.terminated"

    RUNTIME_READY = "runtime.ready"
    RUNTIME_DEGRADED = "runtime.degraded"
    RUNTIME_UNAVAILABLE = "runtime.unavailable"


@dataclass(frozen=True)
class VeyaEvent:
    """Canonical typed event for execution and runtime lifecycle."""

    event_id: str
    execution_id: str
    mission_id: str
    timestamp: float
    actor: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    parent_event_id: str | None = None
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VeyaEvent:
        return cls(**data)


def create_veya_event(
    execution_id: str,
    event_type: str | VeyaEventType,
    *,
    mission_id: str = "",
    actor: str = "system",
    payload: dict[str, Any] | None = None,
    parent_event_id: str | None = None,
    seq: int = 0,
) -> VeyaEvent:
    """Construct a canonical VeyaEvent with unique ID and current timestamp."""
    return VeyaEvent(
        event_id=f"evt_{uuid.uuid4().hex[:16]}",
        execution_id=execution_id,
        mission_id=mission_id,
        timestamp=time.time(),
        actor=actor,
        event_type=str(event_type),
        payload=dict(payload or {}),
        parent_event_id=parent_event_id,
        seq=seq,
    )
