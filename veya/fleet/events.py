"""Fleet Event Model (spec §21).

Invariants:
- Uses canonical event representations; no separate secondary event bus.
- Structured event kinds for agent lifecycle, placement, leases, and migration.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FleetEvent:
    event_id: str
    kind: str
    fleet_id: str
    timestamp: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FleetEventEmitter:
    """Emits fleet lifecycle and placement events."""

    def __init__(self, fleet_id: str):
        self.fleet_id = fleet_id
        self._events: list[FleetEvent] = []

    def emit(self, kind: str, details: dict[str, Any] | None = None) -> FleetEvent:
        import uuid

        evt = FleetEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            kind=kind,
            fleet_id=self.fleet_id,
            timestamp=time.time(),
            details=details or {},
        )
        self._events.append(evt)
        return evt

    def list_events(self, kind: str | None = None) -> list[FleetEvent]:
        if kind is None:
            return list(self._events)
        return [e for e in self._events if e.kind == kind]
