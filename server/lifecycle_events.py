"""server/lifecycle_events.py — LifecycleEvent authority (P0_01_LIFECYCLE_EVENT_BUS).

Single authority for lifecycle facts. The durable journal
(``server.events.EventStore``) and the durable session journal
(``server.session_events.DurableSessionEventStore``) are projections' storage;
SSE (``server.sse``) is a pure projection. Nothing here imports SSE, and SSE
must only read lifecycle state through the projection helpers below — never
as a source of truth for replay.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class DurabilityClass(StrEnum):
    """Persistence semantics per lifecycle event type."""

    DURABLE = "durable"
    REPLAYABLE = "replayable"
    EPHEMERAL = "ephemeral"


SCHEMA_VERSION = 1

TAXONOMY: dict[str, DurabilityClass] = {
    # run/task lifecycle — state transitions are durable
    "run.created": DurabilityClass.DURABLE,
    "run.started": DurabilityClass.DURABLE,
    "run.completed": DurabilityClass.DURABLE,
    "run.failed": DurabilityClass.DURABLE,
    "run.cancelled": DurabilityClass.DURABLE,
    "run.heartbeat": DurabilityClass.EPHEMERAL,
    "task.created": DurabilityClass.DURABLE,
    "task.started": DurabilityClass.DURABLE,
    "task.waiting_approval": DurabilityClass.DURABLE,
    "task.completed": DurabilityClass.DURABLE,
    "task.failed": DurabilityClass.DURABLE,
    "task.cancelled": DurabilityClass.DURABLE,
    # execution lifecycle
    "execution.started": DurabilityClass.DURABLE,
    "execution.progress": DurabilityClass.EPHEMERAL,
    "execution.step_completed": DurabilityClass.REPLAYABLE,
    "execution.completed": DurabilityClass.DURABLE,
    "execution.failed": DurabilityClass.DURABLE,
    # approval/policy lifecycle — decisions are durable, live pings are not
    "approval.requested": DurabilityClass.DURABLE,
    "approval.granted": DurabilityClass.DURABLE,
    "approval.denied": DurabilityClass.DURABLE,
    "policy.decision": DurabilityClass.DURABLE,
    "policy.heartbeat": DurabilityClass.EPHEMERAL,
    # workspace lifecycle
    "workspace.created": DurabilityClass.DURABLE,
    "workspace.updated": DurabilityClass.EPHEMERAL,
    "workspace.snapshot": DurabilityClass.DURABLE,
    "workspace.deleted": DurabilityClass.DURABLE,
    # verification/completion lifecycle
    "verification.started": DurabilityClass.REPLAYABLE,
    "verification.passed": DurabilityClass.DURABLE,
    "verification.failed": DurabilityClass.DURABLE,
    "completion.recorded": DurabilityClass.DURABLE,
    # recovery/reconciliation lifecycle
    "recovery.started": DurabilityClass.REPLAYABLE,
    "recovery.decision": DurabilityClass.DURABLE,
    "recovery.completed": DurabilityClass.DURABLE,
    "reconciliation.started": DurabilityClass.REPLAYABLE,
    "reconciliation.completed": DurabilityClass.DURABLE,
    "reconciliation.diverged": DurabilityClass.DURABLE,
}

REPLAYABLE_CLASSES = frozenset({DurabilityClass.DURABLE, DurabilityClass.REPLAYABLE})


@dataclass(frozen=True)
class LifecycleEvent:
    """Canonical lifecycle envelope. The single authority for lifecycle facts."""

    event_id: str
    event_type: str
    occurred_at: float
    causation_id: str | None
    correlation_id: str
    session_id: str
    goal_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    durability_class: DurabilityClass = DurabilityClass.DURABLE
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["durability_class"] = self.durability_class.value
        return data


def validate_lifecycle_event(event: LifecycleEvent) -> None:
    """Schema validation. Raises ValueError on any violation."""
    if not event.event_id or not isinstance(event.event_id, str):
        raise ValueError("event_id must be a non-empty string")
    if event.event_type not in TAXONOMY:
        raise ValueError(f"unknown lifecycle event_type: {event.event_type!r}")
    expected = TAXONOMY[event.event_type]
    if event.durability_class is not expected:
        raise ValueError(
            f"durability mismatch for {event.event_type!r}: "
            f"got {event.durability_class.value}, taxonomy requires {expected.value}"
        )
    if not isinstance(event.occurred_at, (int, float)) or event.occurred_at <= 0:
        raise ValueError("occurred_at must be a positive timestamp")
    if event.causation_id is not None and not isinstance(event.causation_id, str):
        raise ValueError("causation_id must be a string or None")
    if not event.correlation_id or not isinstance(event.correlation_id, str):
        raise ValueError("correlation_id must be a non-empty string")
    if not event.session_id or not isinstance(event.session_id, str):
        raise ValueError("session_id must be a non-empty string")
    if not isinstance(event.payload, dict):
        raise ValueError("payload must be a dict")
    try:
        json.dumps(event.payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload must be JSON-serializable: {exc}") from exc
    if event.schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {event.schema_version}")


def build_event(
    event_type: str,
    *,
    session_id: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    goal_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    occurred_at: float | None = None,
) -> LifecycleEvent:
    """Construct and validate a LifecycleEvent. Root events self-correlate."""
    if event_type not in TAXONOMY:
        raise ValueError(f"unknown lifecycle event_type: {event_type!r}")
    resolved_id = event_id or str(uuid.uuid4())
    event = LifecycleEvent(
        event_id=resolved_id,
        event_type=event_type,
        occurred_at=occurred_at if occurred_at is not None else time.time(),
        causation_id=causation_id,
        correlation_id=correlation_id or resolved_id,
        session_id=session_id,
        goal_id=goal_id,
        run_id=run_id,
        task_id=task_id,
        payload=dict(payload or {}),
        durability_class=TAXONOMY[event_type],
        schema_version=SCHEMA_VERSION,
    )
    validate_lifecycle_event(event)
    return event


def to_journal_envelope(event: LifecycleEvent) -> dict[str, Any]:
    """Authority -> durable-journal envelope. event_id preserved for dedup."""
    return {
        "event_id": event.event_id,
        "topic": event.event_type,
        "session_id": event.session_id,
        "trace_id": event.correlation_id,
        "task_id": event.task_id,
        "turn_id": None,
        "actor": "lifecycle",
        "payload": {
            "lifecycle": True,
            "lifecycle_version": event.schema_version,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "causation_id": event.causation_id,
            "correlation_id": event.correlation_id,
            "durability_class": event.durability_class.value,
            "session_id": event.session_id,
            "goal_id": event.goal_id,
            "run_id": event.run_id,
            "task_id": event.task_id,
            "data": dict(event.payload),
        },
    }


def coerce_stored_envelope(stored: dict[str, Any]) -> LifecycleEvent:
    """Durable-journal row -> LifecycleEvent. Legacy rows coerce to REPLAYABLE."""
    payload = stored.get("payload") or {}
    if isinstance(payload, dict) and payload.get("lifecycle") is True:
        data = payload.get("data")
        event = LifecycleEvent(
            event_id=str(stored.get("event_id") or payload.get("event_id") or uuid.uuid4()),
            event_type=str(payload.get("event_type") or stored.get("topic")),
            occurred_at=float(payload.get("occurred_at") or stored.get("ts") or time.time()),
            causation_id=payload.get("causation_id"),
            correlation_id=str(
                payload.get("correlation_id") or stored.get("trace_id") or stored.get("event_id")
            ),
            session_id=str(payload.get("session_id") or stored.get("session_id") or "unknown"),
            goal_id=payload.get("goal_id"),
            run_id=payload.get("run_id"),
            task_id=payload.get("task_id") or stored.get("task_id"),
            payload=dict(data) if isinstance(data, dict) else {},
            durability_class=DurabilityClass(str(payload.get("durability_class", "replayable"))),
            schema_version=int(payload.get("lifecycle_version", SCHEMA_VERSION)),
        )
    else:
        topic = str(stored.get("topic") or stored.get("type") or "unknown")
        event = LifecycleEvent(
            event_id=str(stored.get("event_id") or uuid.uuid4()),
            event_type=topic if topic in TAXONOMY else "execution.progress",
            occurred_at=float(stored.get("ts") or time.time()),
            causation_id=None,
            correlation_id=str(
                stored.get("trace_id") or stored.get("session_id") or stored.get("event_id")
            ),
            session_id=str(stored.get("session_id") or "unknown"),
            goal_id=None,
            run_id=None,
            task_id=stored.get("task_id"),
            payload={k: v for k, v in stored.items() if k != "payload"},
            durability_class=DurabilityClass.REPLAYABLE,
            schema_version=SCHEMA_VERSION,
        )
    return event


def project_event_to_sse(event: LifecycleEvent) -> str:
    """Pure projection: LifecycleEvent -> single SSE data frame. No persistence."""
    body = json.dumps(
        {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "causation_id": event.causation_id,
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            "goal_id": event.goal_id,
            "run_id": event.run_id,
            "task_id": event.task_id,
            "payload": event.payload,
            "durability_class": event.durability_class.value,
            "schema_version": event.schema_version,
        },
        ensure_ascii=False,
    )
    return f"id: {event.event_id}\nevent: {event.event_type}\ndata: {body}\n\n"


def build_session_projection(events: list[LifecycleEvent]) -> dict[str, Any]:
    """Pure fold: ordered replayable events -> session projection. No I/O."""
    projection: dict[str, Any] = {
        "runs": {},
        "tasks": {},
        "approvals": [],
        "verifications": [],
        "recoveries": [],
        "reconciliations": [],
        "workspaces": {},
        "replayed": 0,
    }
    for event in events:
        if event.durability_class not in REPLAYABLE_CLASSES:
            continue
        projection["replayed"] += 1
        kind, _, outcome = event.event_type.partition(".")
        if kind in ("run", "task"):
            bucket = projection["runs"] if kind == "run" else projection["tasks"]
            if kind == "run":
                key = event.run_id or event.task_id or event.event_id
            else:
                key = event.task_id or event.run_id or event.event_id
            bucket[key] = {
                "status": outcome,
                "event_id": event.event_id,
                "occurred_at": event.occurred_at,
                "correlation_id": event.correlation_id,
            }
        elif kind == "approval" or event.event_type == "policy.decision":
            projection["approvals"].append(event.event_id)
        elif kind == "verification":
            projection["verifications"].append({"event_id": event.event_id, "outcome": outcome})
        elif kind == "recovery":
            projection["recoveries"].append(
                {
                    "event_id": event.event_id,
                    "outcome": outcome,
                    "causation_id": event.causation_id,
                    "correlation_id": event.correlation_id,
                }
            )
        elif kind == "reconciliation":
            projection["reconciliations"].append({"event_id": event.event_id, "outcome": outcome})
        elif kind == "workspace":
            projection["workspaces"][event.session_id] = {
                "status": outcome,
                "event_id": event.event_id,
            }
        elif kind == "execution" or kind == "completion":
            projection.setdefault("executions", []).append(event.event_id)
    return projection


class LifecycleEventBus:
    """The single lifecycle authority. Persists via the durable journal only."""

    def __init__(self, journal_path: str | Path | None = None):
        from server.events import EventStore

        self._store = EventStore(path=journal_path)
        self._seen: dict[str, LifecycleEvent] = {}
        self._ephemeral: list[LifecycleEvent] = []

    @property
    def journal_path(self) -> Path:
        return self._store.path

    def emit(
        self,
        event_type: str,
        *,
        session_id: str,
        payload: dict[str, Any] | None = None,
        event_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        goal_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> LifecycleEvent:
        event = build_event(
            event_type,
            session_id=session_id,
            payload=payload,
            event_id=event_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            goal_id=goal_id,
            run_id=run_id,
            task_id=task_id,
        )
        return self.publish(event)

    def emit_child(
        self,
        parent: LifecycleEvent,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
        goal_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> LifecycleEvent:
        """Child inherits the root correlation; causation points at the parent."""
        return self.emit(
            event_type,
            session_id=session_id or parent.session_id,
            payload=payload,
            causation_id=parent.event_id,
            correlation_id=parent.correlation_id,
            goal_id=goal_id if goal_id is not None else parent.goal_id,
            run_id=run_id if run_id is not None else parent.run_id,
            task_id=task_id if task_id is not None else parent.task_id,
        )

    def emit_legacy(
        self,
        topic: str,
        *,
        session_id: str,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> LifecycleEvent:
        """Backward-compat entry: unknown legacy topics coerce to replayable."""
        from server.events import append_canonical_event

        if topic in TAXONOMY:
            return self.emit(topic, session_id=session_id, payload=payload, task_id=task_id)
        stored = append_canonical_event(
            topic, dict(payload or {}), session_id=session_id, task_id=task_id
        )
        return coerce_stored_envelope(stored)

    def publish(self, event: LifecycleEvent) -> LifecycleEvent:
        """Idempotent publish: durable write-ahead, ephemeral stays in memory."""
        validate_lifecycle_event(event)
        if event.event_id in self._seen:
            return self._seen[event.event_id]
        if event.durability_class in REPLAYABLE_CLASSES:
            from server.events import append_lifecycle_envelope

            append_lifecycle_envelope(self._store, to_journal_envelope(event))
        else:
            self._ephemeral.append(event)
        self._seen[event.event_id] = event
        return event

    def replay(
        self,
        *,
        session_id: str | None = None,
        event_types: set[str] | None = None,
    ) -> list[LifecycleEvent]:
        """Rebuild from the durable journal only. Ephemeral is never replayed."""
        rows = self._store.read_all(session_id=session_id)
        events: list[LifecycleEvent] = []
        for row in rows:
            event = coerce_stored_envelope(row)
            if event.durability_class not in REPLAYABLE_CLASSES:
                continue
            if event_types is not None and event.event_type not in event_types:
                continue
            if event.event_id not in self._seen:
                self._seen[event.event_id] = event
            events.append(event)
        events.sort(key=lambda e: e.occurred_at)
        return events

    def rebuild_projection(self, *, session_id: str | None = None) -> dict[str, Any]:
        return build_session_projection(self.replay(session_id=session_id))

    def replay_from_sse_history(self, frames: list[str]) -> list[LifecycleEvent]:
        """Forbidden: SSE is a projection and can never source a replay."""
        raise RuntimeError(
            "replay from SSE history is forbidden: SSE is a lossy projection, "
            f"replay must read the durable journal (rejected {len(frames)} frames)"
        )
