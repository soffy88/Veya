"""Durable execution-scoped output/event log."""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

class ExecutionEventError(ValueError):
    pass

class EventSequenceError(ExecutionEventError):
    pass

class ExecutionEventType:
    PROVIDER_STARTED = "PROVIDER_STARTED"
    OUTPUT = "OUTPUT"
    COMPLETED = "COMPLETED"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_FAILED = "TOOL_FAILED"
    PROGRESS = "PROGRESS"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    ERROR = "ERROR"
    WARNING = "WARNING"
    EXECUTION_STATE_CHANGED = "EXECUTION_STATE_CHANGED"
    BACKPRESSURE = "BACKPRESSURE"

@dataclass(frozen=True)
class ExecutionEvent:
    execution_id: str
    sequence: int
    event_id: str
    event_type: str
    occurred_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    payload_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionEvent":
        return cls(**data)

class ExecutionEventStore:
    """Append-only durable stream with bounded hot-buffer and replay."""

    def __init__(self, root: str | Path, execution_id: str, *, max_buffer_events: int = 256):
        if not execution_id:
            raise ValueError("execution_id is required")
        if max_buffer_events < 1:
            raise ValueError("max_buffer_events must be >= 1")
        self.execution_id = execution_id
        self.max_buffer_events = max_buffer_events
        self._lock = threading.RLock()
        self._events: deque[ExecutionEvent] = deque(maxlen=max_buffer_events)
        self._last_sequence = 0
        self._event_ids: set[str] = set()
        self._path = Path(root) / ".veya" / "execution-events" / f"{execution_id}.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._load_durable()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._last_sequence

    @property
    def buffered_count(self) -> int:
        with self._lock:
            return len(self._events)

    def append(self, event_type: str, *, sequence: int | None = None,
               event_id: str | None = None, payload: dict[str, Any] | None = None,
               payload_ref: str | None = None) -> ExecutionEvent:
        with self._lock:
            event_id = event_id or str(uuid.uuid4())
            if event_id in self._event_ids:
                existing = self._find_event_id(event_id)
                if existing is not None and self._same_event(existing, event_type, payload, payload_ref, sequence):
                    return existing
                raise EventSequenceError(f"event_id collision: {event_id}")
            expected = self._last_sequence + 1
            resolved_sequence = expected if sequence is None else sequence
            if resolved_sequence != expected:
                raise EventSequenceError(
                    f"execution {self.execution_id}: expected sequence {expected}, got {resolved_sequence}"
                )
            event = ExecutionEvent(
                execution_id=self.execution_id,
                sequence=resolved_sequence,
                event_id=event_id,
                event_type=event_type,
                occurred_at=datetime.now(timezone.utc).isoformat(),
                payload=dict(payload or {}),
                payload_ref=payload_ref,
            )
            encoded = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._events.append(event)
            self._event_ids.add(event_id)
            self._last_sequence = resolved_sequence
            return event

    def append_output(self, text: str, *, payload_ref: str | None = None) -> ExecutionEvent:
        return self.append(ExecutionEventType.OUTPUT, payload={"text": text}, payload_ref=payload_ref)

    def replay_after(self, last_seen_sequence: int = 0, *, limit: int | None = None) -> list[ExecutionEvent]:
        if last_seen_sequence < 0:
            raise ValueError("last_seen_sequence must be >= 0")
        with self._lock:
            events = [event for event in self._events if event.sequence > last_seen_sequence]
            oldest_buffered = self._last_sequence - len(self._events) + 1
            if last_seen_sequence < oldest_buffered - 1:
                events = self._read_durable_after(last_seen_sequence)
            if limit is not None:
                if limit < 1:
                    raise ValueError("limit must be >= 1")
                events = events[:limit]
            return events

    def emit_backpressure(self, *, buffered: int, capacity: int) -> ExecutionEvent:
        return self.append(
            ExecutionEventType.BACKPRESSURE,
            payload={"buffered": buffered, "capacity": capacity},
        )

    def _load_durable(self) -> None:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = ExecutionEvent.from_dict(json.loads(line))
                if event.execution_id != self.execution_id or event.sequence != self._last_sequence + 1:
                    raise EventSequenceError(f"corrupt execution event log: {self._path}")
                self._events.append(event)
                self._event_ids.add(event.event_id)
                self._last_sequence = event.sequence

    def _read_durable_after(self, sequence: int) -> list[ExecutionEvent]:
        result = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    event = ExecutionEvent.from_dict(json.loads(line))
                    if event.sequence > sequence:
                        result.append(event)
        return result

    def _find_event_id(self, event_id: str) -> ExecutionEvent | None:
        for event in self._events:
            if event.event_id == event_id:
                return event
        if self._path.exists():
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        data = json.loads(line)
                        if data["event_id"] == event_id:
                            return ExecutionEvent.from_dict(data)
        return None

    @staticmethod
    def _same_event(existing: ExecutionEvent, event_type: str,
                    payload: dict[str, Any] | None, payload_ref: str | None,
                    sequence: int | None) -> bool:
        return (existing.event_type == event_type
                and existing.payload == dict(payload or {})
                and existing.payload_ref == payload_ref
                and (sequence is None or existing.sequence == sequence))
